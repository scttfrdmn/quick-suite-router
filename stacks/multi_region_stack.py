"""
Quick Suite Model Router — Multi-Region Failover Stack

Creates Route 53 health-check failover records so traffic automatically
shifts to a secondary API Gateway endpoint if the primary becomes unhealthy.

Deploy alongside the main stack with:
  cdk deploy --context secondary_region=us-west-2

Resources:
  - Route 53 health check targeting the primary API Gateway domain
  - Route 53 failover record set pair (PRIMARY + SECONDARY)

Prerequisites:
  - A Route 53 hosted zone for your custom domain
  - A secondary ModelRouterStack deployed in the secondary region
  - Set context vars: hosted_zone_id, hosted_zone_name, primary_domain,
    secondary_domain (all required when deploying multi-region)
"""

from aws_cdk import Stack
from aws_cdk import aws_route53 as route53
from constructs import Construct


class MultiRegionStack(Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        primary_api_url: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        # Required context vars for multi-region deployment
        hosted_zone_id = self.node.try_get_context("hosted_zone_id") or ""
        hosted_zone_name = self.node.try_get_context("hosted_zone_name") or ""
        primary_domain = self.node.try_get_context("primary_domain") or ""
        secondary_domain = self.node.try_get_context("secondary_domain") or ""

        if not all([hosted_zone_id, hosted_zone_name, primary_domain, secondary_domain]):
            # Print guidance but do not fail synth — allows the stack to be
            # included in the app without providing all context vars upfront.
            return

        # Parse the primary API Gateway hostname from the URL
        # e.g. https://abc123.execute-api.us-east-1.amazonaws.com/prod/
        primary_hostname = primary_api_url.replace("https://", "").split("/")[0]


        # Route 53 health check — monitors the primary API Gateway /health endpoint
        health_check = route53.CfnHealthCheck(
            self,
            "PrimaryHealthCheck",
            health_check_config=route53.CfnHealthCheck.HealthCheckConfigProperty(
                type="HTTPS",
                fully_qualified_domain_name=primary_hostname,
                resource_path="/prod/health",
                port=443,
                request_interval=30,
                failure_threshold=3,
            ),
            health_check_tags=[
                route53.CfnHealthCheck.HealthCheckTagProperty(
                    key="Name",
                    value="qs-model-router-primary",
                )
            ],
        )

        # PRIMARY failover record — points at primary region API Gateway
        route53.CfnRecordSet(
            self,
            "PrimaryRecord",
            hosted_zone_id=hosted_zone_id,
            name=f"model-router.{hosted_zone_name}",
            type="CNAME",
            ttl="60",
            resource_records=[primary_domain],
            failover="PRIMARY",
            set_identifier="primary",
            health_check_id=health_check.ref,
        )

        # SECONDARY failover record — points at secondary region API Gateway
        route53.CfnRecordSet(
            self,
            "SecondaryRecord",
            hosted_zone_id=hosted_zone_id,
            name=f"model-router.{hosted_zone_name}",
            type="CNAME",
            ttl="60",
            resource_records=[secondary_domain],
            failover="SECONDARY",
            set_identifier="secondary",
        )
