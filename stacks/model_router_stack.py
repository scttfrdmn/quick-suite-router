"""
Quick Suite Multi-Model Router — CDK Stack

Deploys a complete multi-provider LLM routing infrastructure that integrates
with Amazon Quick Suite via Bedrock AgentCore Gateway.

Provisions:
  - Cognito User Pool + App Client (Quick Suite / AgentCore auth)
  - Secrets Manager entries (per-provider API keys)
  - Lambda functions (router + 4 provider handlers)
  - Lambda Layer (shared provider interface + governance)
  - Bedrock Guardrail (content filtering for ALL providers)
  - API Gateway (HTTP backend for AgentCore Gateway)
  - DynamoDB response cache (optional, with configurable TTL)
  - CloudWatch dashboard (usage metering across all providers)
  - IAM roles with least-privilege policies

AgentCore Integration:
  After deployment, register the API Gateway endpoint as an AgentCore
  Gateway target. Quick Suite connects to AgentCore via MCP Actions
  Integration. See docs/quicksuite-integration.md for step-by-step.
"""

import json
from pathlib import Path

import aws_cdk as cdk
import yaml
from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_apigateway as apigw,
)
from aws_cdk import (
    aws_bedrock as bedrock,
)
from aws_cdk import (
    aws_cloudwatch as cw,
)
from aws_cdk import (
    aws_cloudwatch_actions as cw_actions,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from aws_cdk import (
    aws_sns as sns,
)
from aws_cdk import (
    aws_sns_subscriptions as sns_subs,
)
from constructs import Construct


class ModelRouterStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        prefix = "qs-model-router"

        # Feature flags from CDK context
        enable_cache = self.node.try_get_context("enable_cache")
        if enable_cache is None:
            enable_cache = True
        cache_ttl_minutes = int(
            self.node.try_get_context("cache_ttl_minutes") or 60
        )
        guardrail_version = self.node.try_get_context("guardrail_version") or "DRAFT"

        # -----------------------------------------------------------------
        # Routing config
        # -----------------------------------------------------------------
        config_path = (
            Path(__file__).parent.parent / "config" / "routing_config.yaml"
        )
        if config_path.exists():
            with open(config_path) as f:
                routing_config = yaml.safe_load(f)
        else:
            routing_config = self._default_routing_config()

        # -----------------------------------------------------------------
        # Secrets Manager — one per external provider (Bedrock uses IAM)
        # -----------------------------------------------------------------
        secrets = {}
        for provider in ["anthropic", "openai", "gemini"]:
            secrets[provider] = secretsmanager.Secret(
                self,
                f"{provider}-secret",
                secret_name=f"quicksuite-model-router/{provider}",
                description=(
                    f"API credentials for {provider} provider. "
                    f"Populate after deployment — see docs/setup-{provider}.md"
                ),
                removal_policy=RemovalPolicy.RETAIN,
            )

        # -----------------------------------------------------------------
        # Cognito User Pool — AgentCore / Quick Suite OAuth 2.0
        # -----------------------------------------------------------------
        user_pool = cognito.UserPool(
            self,
            "AuthPool",
            user_pool_name=f"{prefix}-auth",
            self_sign_up_enabled=False,
            removal_policy=RemovalPolicy.DESTROY,
        )

        resource_server = user_pool.add_resource_server(
            "ResourceServer",
            identifier="model-router",
            scopes=[
                cognito.ResourceServerScope(
                    scope_name="invoke",
                    scope_description="Invoke model router tools",
                )
            ],
        )

        app_client = user_pool.add_client(
            "AgentCoreClient",
            user_pool_client_name=f"{prefix}-agentcore",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[
                    cognito.OAuthScope.resource_server(
                        resource_server,
                        cognito.ResourceServerScope(
                            scope_name="invoke",
                            scope_description="Invoke model router tools",
                        ),
                    )
                ],
            ),
        )

        # Cognito domain prefix must be static (no CloudFormation tokens).
        # Use a hash of the construct path for a stable, unique suffix.
        cognito_domain_prefix = (
            self.node.try_get_context("cognito_domain_prefix")
            or f"{prefix}-{cdk.Names.unique_id(self)[:8].lower()}"
        )
        domain = user_pool.add_domain(
            "CognitoDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=cognito_domain_prefix,
            ),
        )

        # -----------------------------------------------------------------
        # Bedrock Guardrail — applied to ALL provider calls
        # -----------------------------------------------------------------
        guardrail = bedrock.CfnGuardrail(
            self,
            "ContentGuardrail",
            name=f"{prefix}-guardrail",
            blocked_input_messaging="Request blocked by content policy.",
            blocked_outputs_messaging="Response blocked by content policy.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=t,
                        input_strength=s,
                        output_strength=s,
                    )
                    for t, s in [
                        ("SEXUAL", "HIGH"),
                        ("VIOLENCE", "HIGH"),
                        ("HATE", "HIGH"),
                        ("INSULTS", "MEDIUM"),
                        ("MISCONDUCT", "HIGH"),
                    ]
                ]
                + [
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK",
                        input_strength="HIGH",
                        output_strength="NONE",
                    )
                ],
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="EMAIL", action="ANONYMIZE"
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="PHONE", action="ANONYMIZE"
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="US_SOCIAL_SECURITY_NUMBER", action="BLOCK"
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="CREDIT_DEBIT_CARD_NUMBER", action="BLOCK"
                    ),
                ],
            ),
        )

        # -----------------------------------------------------------------
        # DynamoDB Response Cache (optional)
        # -----------------------------------------------------------------
        cache_table = None
        cache_table_name = ""

        if enable_cache:
            cache_table = dynamodb.Table(
                self,
                "ResponseCache",
                table_name=f"{prefix}-cache",
                partition_key=dynamodb.Attribute(
                    name="cache_key", type=dynamodb.AttributeType.STRING
                ),
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                removal_policy=RemovalPolicy.DESTROY,
                time_to_live_attribute="ttl",
            )
            cache_table_name = cache_table.table_name

        # -----------------------------------------------------------------
        # Shared Lambda Layer
        # -----------------------------------------------------------------
        common_layer = lambda_.LayerVersion(
            self,
            "CommonLayer",
            code=lambda_.Code.from_asset("lambdas/common"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Shared provider interface and governance utilities",
        )

        # -----------------------------------------------------------------
        # Provider Lambdas
        # -----------------------------------------------------------------

        # --- Bedrock provider (IAM auth, no external secret) ---
        bedrock_role = iam.Role(
            self,
            "BedrockProviderRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        bedrock_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=["arn:aws:bedrock:*::foundation-model/*"],
            )
        )
        bedrock_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail.attr_guardrail_arn],
            )
        )

        provider_lambdas = {}

        provider_lambdas["bedrock"] = lambda_.Function(
            self,
            "BedrockProvider",
            function_name=f"{prefix}-provider-bedrock",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="bedrock_provider.handler",
            code=lambda_.Code.from_asset("lambdas/providers"),
            layers=[common_layer],
            role=bedrock_role,
            timeout=Duration.seconds(120),
            memory_size=512,
            tracing=lambda_.Tracing.ACTIVE,
            environment={
                "GUARDRAIL_ID": guardrail.attr_guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version,
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # --- External providers (Anthropic, OpenAI, Gemini) ---
        for provider_name in ["anthropic", "openai", "gemini"]:
            fn = lambda_.Function(
                self,
                f"{provider_name.title()}Provider",
                function_name=f"{prefix}-provider-{provider_name}",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=f"{provider_name}_provider.handler",
                code=lambda_.Code.from_asset("lambdas/providers"),
                layers=[common_layer],
                timeout=Duration.seconds(120),
                memory_size=512,
                tracing=lambda_.Tracing.ACTIVE,
                environment={
                    "SECRET_ARN": secrets[provider_name].secret_arn,
                    "GUARDRAIL_ID": guardrail.attr_guardrail_id,
                    "GUARDRAIL_VERSION": guardrail_version,
                },
                log_retention=logs.RetentionDays.ONE_MONTH,
            )
            secrets[provider_name].grant_read(fn)
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[guardrail.attr_guardrail_arn],
                )
            )
            provider_lambdas[provider_name] = fn

        # -----------------------------------------------------------------
        # Router Lambda
        # -----------------------------------------------------------------
        router_role = iam.Role(
            self,
            "RouterRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        for name, fn in provider_lambdas.items():
            fn.grant_invoke(router_role)

        for secret in secrets.values():
            secret.grant_read(router_role)

        router_env = {
            "ROUTING_CONFIG": json.dumps(routing_config),
            "PROVIDER_FUNCTIONS": json.dumps(
                {n: fn.function_arn for n, fn in provider_lambdas.items()}
            ),
            "PROVIDER_SECRETS": json.dumps(
                {n: s.secret_arn for n, s in secrets.items()}
            ),
            "CACHE_TABLE": cache_table_name,
            "CACHE_TTL_MINUTES": str(cache_ttl_minutes),
        }

        router_fn = lambda_.Function(
            self,
            "RouterFunction",
            function_name=f"{prefix}-router",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("lambdas/router"),
            layers=[common_layer],
            role=router_role,
            timeout=Duration.seconds(30),
            memory_size=512,
            tracing=lambda_.Tracing.ACTIVE,
            environment=router_env,
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        if cache_table:
            cache_table.grant_read_write_data(router_fn)

        # -----------------------------------------------------------------
        # API Gateway — HTTP backend for AgentCore Gateway
        #
        # AgentCore Gateway connects to this endpoint as a target.
        # Quick Suite → AgentCore Gateway (MCP) → API Gateway → Router Lambda
        # -----------------------------------------------------------------
        api = apigw.RestApi(
            self,
            "ModelRouterApi",
            rest_api_name=f"{prefix}-api",
            description=(
                "HTTP backend for Quick Suite Model Router. "
                "Register as an AgentCore Gateway target."
            ),
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                throttling_rate_limit=100,
                throttling_burst_limit=50,
                logging_level=apigw.MethodLoggingLevel.INFO,
                data_trace_enabled=bool(self.node.try_get_context("api_data_trace")),
            ),
        )

        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self,
            "CognitoAuth",
            cognito_user_pools=[user_pool],
        )

        # Tool endpoints — these become MCP tools in AgentCore
        tools_resource = api.root.add_resource("tools")
        for tool_name in ["analyze", "generate", "research", "summarize", "code"]:
            tool_resource = tools_resource.add_resource(tool_name)
            tool_resource.add_method(
                "POST",
                apigw.LambdaIntegration(
                    router_fn,
                    request_templates={
                        "application/json": json.dumps(
                            {
                                "tool": tool_name,
                                "body": "$util.escapeJavaScript($input.body)",
                            }
                        )
                    },
                ),
                authorizer=authorizer,
                authorization_type=apigw.AuthorizationType.COGNITO,
                authorization_scopes=["model-router/invoke"],
            )

        # Health check (unauthenticated)
        health = api.root.add_resource("health")
        health.add_method(
            "GET",
            apigw.MockIntegration(
                integration_responses=[
                    apigw.IntegrationResponse(
                        status_code="200",
                        response_templates={
                            "application/json": '{"status": "healthy"}'
                        },
                    )
                ],
                request_templates={"application/json": '{"statusCode": 200}'},
            ),
            method_responses=[apigw.MethodResponse(status_code="200")],
        )

        # Status endpoint (authenticated — shows provider availability)
        status = api.root.add_resource("status")
        status.add_method(
            "GET",
            apigw.LambdaIntegration(router_fn),
            authorizer=authorizer,
            authorization_type=apigw.AuthorizationType.COGNITO,
        )

        # -----------------------------------------------------------------
        # API Usage Plan + API Key (rate limiting)
        # -----------------------------------------------------------------
        throttle_rate = int(self.node.try_get_context("api_throttle_rate") or 100)
        throttle_burst = int(self.node.try_get_context("api_throttle_burst") or 200)

        api_key = api.add_api_key(
            "ModelRouterApiKey",
            api_key_name=f"{prefix}-api-key",
            description="Quick Suite Model Router API key",
        )
        usage_plan = api.add_usage_plan(
            "ModelRouterUsagePlan",
            name=f"{prefix}-usage-plan",
            throttle=apigw.ThrottleSettings(
                rate_limit=throttle_rate,
                burst_limit=throttle_burst,
            ),
        )
        usage_plan.add_api_key(api_key)
        usage_plan.add_api_stage(stage=api.deployment_stage)

        # -----------------------------------------------------------------
        # CloudWatch Dashboard
        # -----------------------------------------------------------------
        dashboard = cw.Dashboard(
            self,
            "UsageDashboard",
            dashboard_name=f"{prefix}-usage",
        )

        dashboard.add_widgets(
            cw.GraphWidget(
                title="Invocations by provider",
                left=[
                    fn.metric_invocations(label=name, period=Duration.hours(1))
                    for name, fn in provider_lambdas.items()
                ],
                width=12,
            ),
            cw.GraphWidget(
                title="Latency p50 / p99",
                left=[
                    fn.metric_duration(
                        label=f"{name} p50",
                        statistic="p50",
                        period=Duration.hours(1),
                    )
                    for name, fn in provider_lambdas.items()
                ],
                right=[
                    fn.metric_duration(
                        label=f"{name} p99",
                        statistic="p99",
                        period=Duration.hours(1),
                    )
                    for name, fn in provider_lambdas.items()
                ],
                width=12,
            ),
        )

        dashboard.add_widgets(
            cw.GraphWidget(
                title="Token usage by provider",
                left=[
                    cw.Metric(
                        namespace="QuickSuiteModelRouter",
                        metric_name="InputTokens",
                        dimensions_map={"Provider": name},
                        period=Duration.hours(1),
                        statistic="Sum",
                        label=f"{name} input",
                    )
                    for name in provider_lambdas.keys()
                ],
                right=[
                    cw.Metric(
                        namespace="QuickSuiteModelRouter",
                        metric_name="OutputTokens",
                        dimensions_map={"Provider": name},
                        period=Duration.hours(1),
                        statistic="Sum",
                        label=f"{name} output",
                    )
                    for name in provider_lambdas.keys()
                ],
                width=12,
            ),
            cw.GraphWidget(
                title="Errors and guardrail blocks",
                left=[
                    fn.metric_errors(label=name, period=Duration.hours(1))
                    for name, fn in provider_lambdas.items()
                ],
                right=[
                    cw.Metric(
                        namespace="QuickSuiteModelRouter",
                        metric_name="GuardrailBlocked",
                        period=Duration.hours(1),
                        statistic="Sum",
                    )
                ],
                width=12,
            ),
        )

        dashboard.add_widgets(
            cw.GraphWidget(
                title="Guardrail coverage",
                left=[
                    cw.Metric(
                        namespace="QuickSuiteModelRouter",
                        metric_name="GuardrailApplied",
                        period=Duration.hours(1),
                        statistic="Sum",
                        label="Applied",
                    ),
                    cw.Metric(
                        namespace="QuickSuiteModelRouter",
                        metric_name="GuardrailBlocked",
                        period=Duration.hours(1),
                        statistic="Sum",
                        label="Blocked",
                    ),
                ],
                width=12,
            ),
        )

        if cache_table:
            dashboard.add_widgets(
                cw.GraphWidget(
                    title="Cache hit rate",
                    left=[
                        cw.Metric(
                            namespace="QuickSuiteModelRouter",
                            metric_name="CacheHit",
                            period=Duration.hours(1),
                            statistic="Sum",
                            label="Hits",
                        ),
                        cw.Metric(
                            namespace="QuickSuiteModelRouter",
                            metric_name="CacheMiss",
                            period=Duration.hours(1),
                            statistic="Sum",
                            label="Misses",
                        ),
                    ],
                    width=12,
                ),
            )

        # -----------------------------------------------------------------
        # CloudWatch Alarms
        # -----------------------------------------------------------------
        alarm_email = self.node.try_get_context("alarm_email")
        alarm_topic = None
        if alarm_email:
            alarm_topic = sns.Topic(
                self,
                "AlarmTopic",
                topic_name=f"{prefix}-alarms",
                display_name="Quick Suite Model Router Alarms",
            )
            alarm_topic.add_subscription(sns_subs.EmailSubscription(alarm_email))

        def _add_sns_action(alarm):
            if alarm_topic:
                alarm.add_alarm_action(cw_actions.SnsAction(alarm_topic))

        # Alarm 1 — p99 latency > 5 s on the router Lambda
        latency_alarm = cw.Alarm(
            self,
            "LatencyAlarm",
            alarm_name=f"{prefix}-latency-p99",
            alarm_description="Router Lambda p99 latency exceeded 5 seconds",
            metric=router_fn.metric_duration(
                period=Duration.minutes(5),
                statistic="p99",
            ),
            threshold=5000,
            evaluation_periods=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        _add_sns_action(latency_alarm)

        # Alarm 2 — error rate > 5 %  (errors / invocations * 100)
        error_rate_metric = cw.MathExpression(
            expression="100 * errors / MAX([errors, invocations])",
            using_metrics={
                "errors": router_fn.metric_errors(period=Duration.minutes(5)),
                "invocations": router_fn.metric_invocations(period=Duration.minutes(5)),
            },
            label="Router error rate %",
            period=Duration.minutes(5),
        )
        error_alarm = cw.Alarm(
            self,
            "ErrorRateAlarm",
            alarm_name=f"{prefix}-error-rate",
            alarm_description="Router Lambda error rate exceeded 5 %",
            metric=error_rate_metric,
            threshold=5,
            evaluation_periods=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        _add_sns_action(error_alarm)

        # Alarm 3 — fallback rate > 20 %
        fallback_rate_metric = cw.MathExpression(
            expression="100 * fallbacks / MAX([fallbacks, invocations])",
            using_metrics={
                "fallbacks": cw.Metric(
                    namespace="QuickSuiteModelRouter",
                    metric_name="FallbackInvoked",
                    period=Duration.minutes(5),
                    statistic="Sum",
                ),
                "invocations": router_fn.metric_invocations(period=Duration.minutes(5)),
            },
            label="Fallback rate %",
            period=Duration.minutes(5),
        )
        fallback_alarm = cw.Alarm(
            self,
            "FallbackRateAlarm",
            alarm_name=f"{prefix}-fallback-rate",
            alarm_description="Provider fallback rate exceeded 20 % of router invocations",
            metric=fallback_rate_metric,
            threshold=20,
            evaluation_periods=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        _add_sns_action(fallback_alarm)

        # -----------------------------------------------------------------
        # Outputs
        # -----------------------------------------------------------------
        token_url = domain.base_url() + "/oauth2/token"

        CfnOutput(
            self,
            "ApiEndpoint",
            value=api.url,
            description="API Gateway endpoint — register as AgentCore Gateway target",
        )
        CfnOutput(
            self,
            "CognitoUserPoolId",
            value=user_pool.user_pool_id,
        )
        CfnOutput(
            self,
            "CognitoClientId",
            value=app_client.user_pool_client_id,
            description="Client ID for AgentCore / Quick Suite auth",
        )
        CfnOutput(
            self,
            "CognitoTokenUrl",
            value=token_url,
            description="Token endpoint for OAuth",
        )
        CfnOutput(
            self,
            "GuardrailId",
            value=guardrail.attr_guardrail_id,
        )
        CfnOutput(
            self,
            "DashboardUrl",
            value=(
                f"https://{self.region}.console.aws.amazon.com"
                f"/cloudwatch/home#dashboards:name={prefix}-usage"
            ),
        )

        CfnOutput(
            self,
            "ApiKeyId",
            value=api_key.key_id,
            description="API Gateway API key ID (retrieve value via console or AWS CLI)",
        )

        # Expose for cross-stack references (multi-region stack)
        self.api_url = api.url

        if cache_table:
            CfnOutput(
                self,
                "CacheTableName",
                value=cache_table.table_name,
                description="DynamoDB response cache table",
            )

        # Convenience: secrets ARNs for populating after deploy
        for provider_name, secret in secrets.items():
            CfnOutput(
                self,
                f"{provider_name.title()}SecretArn",
                value=secret.secret_arn,
                description=f"Populate with {provider_name} API key",
            )

    @staticmethod
    def _default_routing_config() -> dict:
        return {
            "routing": {
                "analyze": {
                    "preferred": [
                        "bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
                        "anthropic/claude-sonnet-4-20250514",
                        "openai/gpt-4o",
                        "gemini/gemini-2.5-pro",
                    ],
                    "system_prompt": "You are an expert analyst. Provide thorough, well-structured analysis with clear reasoning.",
                },
                "generate": {
                    "preferred": [
                        "bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
                        "openai/gpt-4o",
                        "gemini/gemini-2.5-pro",
                    ],
                    "system_prompt": "You are a skilled content creator. Generate high-quality, well-structured content.",
                },
                "research": {
                    "preferred": [
                        "bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
                        "gemini/gemini-2.5-pro",
                        "openai/gpt-4o",
                    ],
                    "system_prompt": "You are a research assistant. Synthesize information thoroughly with clear reasoning.",
                },
                "summarize": {
                    "preferred": [
                        "bedrock/amazon.nova-pro-v1:0",
                        "openai/gpt-4o-mini",
                        "gemini/gemini-2.5-flash",
                    ],
                    "system_prompt": "You are a concise summarizer. Extract key points clearly and briefly.",
                },
                "code": {
                    "preferred": [
                        "bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
                        "anthropic/claude-sonnet-4-20250514",
                        "openai/gpt-4o",
                    ],
                    "system_prompt": "You are an expert software engineer. Write clean, correct, well-documented code.",
                },
            },
            "defaults": {
                "max_tokens": 4096,
                "temperature": 0.7,
            },
        }
