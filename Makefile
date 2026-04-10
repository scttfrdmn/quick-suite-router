.PHONY: audit sast scan-fs scan-iac cfn-lint security

audit:
	pip-audit .

sast:
	semgrep scan --config=auto --error .

scan-fs:
	trivy fs --severity HIGH,CRITICAL .

scan-iac:
	@if [ -d cdk.out ]; then trivy config --severity HIGH,CRITICAL cdk.out/; \
	else echo "Run 'cdk synth' first"; exit 1; fi

cfn-lint:
	@if [ -d cdk.out ]; then cfn-lint cdk.out/**/*.template.json --ignore-checks W; \
	else echo "Run 'cdk synth' first"; exit 1; fi

security: audit sast scan-fs scan-iac cfn-lint
