# DRS Replication Lag Monitor

Serverless monitor for AWS Elastic Disaster Recovery (DRS). On a schedule, it
scans DRS source servers, checks their replication lag against a threshold,
and emails an SNS alert listing any servers that are lagging.

## Architecture

EventBridge (every 8 hours) → Lambda → DRS + EC2 APIs → SNS email alert.
Failed Lambda invocations go to an SQS dead-letter queue.

| Resource | Purpose |
|---|---|
| `DRSLagChecker` (Lambda, Python 3.12) | Calls `drs:DescribeSourceServers` per configured region, resolves each source server's EC2 `Name` tag, parses `dataReplicationInfo.lagDuration`, and publishes an SNS alert if any server exceeds `LAG_THRESHOLD_SECONDS`. |
| `DRSMonitorRule` (EventBridge) | Triggers the Lambda on a `rate(8 hours)` schedule. |
| `AlertTopic` (SNS) | Delivers the alert email. |
| `LambdaDLQ` (SQS) | Captures failed Lambda invocations (14-day retention). |
| `LambdaExecutionRole` (IAM) | Least-privilege role: `drs:DescribeSourceServers`, `ec2:DescribeInstances` (read-only, both need `Resource: "*"` since AWS doesn't support scoping these), `sns:Publish` scoped to the alert topic, `sqs:SendMessage` scoped to the DLQ. |

This stack intentionally does not encrypt the SQS queue, SNS topic, or Lambda
environment variables with a customer-managed KMS key — an earlier iteration
had a KMS key and it was deliberately removed to avoid the extra cost/complexity
for a queue and topic that carry no sensitive data (just server IDs, hostnames,
and lag durations). The corresponding Checkov checks (`CKV_AWS_27`,
`CKV_AWS_26`, `CKV_AWS_173`) are explicitly suppressed in
[`cdk/stacks/drs_monitor_stack.py`](cdk/stacks/drs_monitor_stack.py) with that
rationale, so the security CI job passes intentionally rather than by omission.

## This is a CDK project

The stack is defined in Python CDK under [`cdk/`](cdk/):

```
cdk/
├── app.py                    # entrypoint - reads context (alert_email, lag_threshold, drs_regions)
├── cdk.json                  # non-sensitive defaults (lag_threshold, drs_regions)
├── requirements.txt          # aws-cdk-lib, constructs
├── stacks/drs_monitor_stack.py  # the stack: DLQ, topic, role, function, rule
└── lambda/index.py           # Lambda handler code, bundled as a CDK asset
```

> `drs-lag-monitor.yaml` (the old CloudFormation template) and the root
> `lambda.py` are kept temporarily for reference during the cutover to the new
> stack — see [Migration status](#migration-status) below. Once the old stack
> is decommissioned, both files should be deleted.

### Prerequisites

- AWS CLI credentials for the target account/region (`991587605613` / `eu-west-1`).
- Node.js + the CDK CLI (`npm install -g aws-cdk`), Python 3.12.
- **One-time per account/region**: `cdk bootstrap aws://991587605613/eu-west-1`
  — creates the CDK asset S3 bucket, optional ECR repo, and the bootstrap IAM
  roles CDK uses to deploy. Safe to re-run if you're unsure whether it's done.
- The CI deploy role (`arn:aws:iam::991587605613:role/GitHub-Deploy-Role`) must
  be allowed to `sts:AssumeRole` into the CDK bootstrap roles
  (`cdk-hnb659fds-deploy-role-...`, `cdk-hnb659fds-file-publishing-role-...`).
  This is an IAM change made in account `991587605613`, outside this repo —
  without it, `cdk deploy` fails with an AssumeRole error in CI.

### Local usage

```bash
cd cdk
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Preview the generated CloudFormation
cdk synth -c alert_email=you@example.com -c lag_threshold=120 -c drs_regions=""

# See what would change against the deployed stack
cdk diff -c alert_email=you@example.com -c lag_threshold=120 -c drs_regions=""

# Deploy
cdk deploy -c alert_email=you@example.com -c lag_threshold=120 -c drs_regions=""
```

Config is passed via CDK context (`-c` flags), not a checked-in file:
`lag_threshold` and `drs_regions` have non-sensitive defaults in `cdk.json`;
`alert_email` is always required explicitly and `app.py` raises if it's
missing. `drs_regions` is a comma-separated list of regions to scan; leave it
empty to scan only the Lambda's own region.

### CI/CD

`.github/workflows/deploy.yaml` has two jobs:

- **`security`** (runs on every push and PR): `cdk synth` with placeholder
  values (no AWS credentials needed), then Checkov against the synthesized
  template in `cdk/cdk.out`, then Bandit against `cdk/lambda`.
- **`deploy`** (push to `main` only, needs `security`): authenticates via
  OIDC as `GitHub-Deploy-Role`, then runs `cdk diff` (informational) and
  `cdk deploy --require-approval never`. There is no manual S3 bucket or zip
  step — CDK packages and uploads the Lambda asset itself via the bootstrap
  asset bucket.

The alert email address is read from the `ALERT_EMAIL` repository/environment
variable (`vars.ALERT_EMAIL` in the workflow), not hardcoded.

## Migration status

This project is migrating from a hand-written CloudFormation template
(`drs-lag-monitor.yaml`, deployed via a manually-managed S3 bucket and
`aws cloudformation deploy`) to this CDK app. The new stack (`DrsMonitorStack`)
uses **new resource names** (e.g. `drs-monitor-lambda-<account>` instead of
`DR-Monitoring-lambda-<account>`) so it can be deployed and verified
side-by-side with the still-running old stack (`drs-lag-monitor`) with zero
monitoring downtime.

Cutover sequence:
1. Deploy `DrsMonitorStack` via CI (merge to `main`).
2. Confirm the new SNS subscription (a new confirmation email is sent to
   `ALERT_EMAIL` — this is expected).
3. Verify: manually invoke the new Lambda, check its CloudWatch Logs, confirm
   the DLQ has no unexpected messages, confirm the EventBridge rule is
   `ENABLED` and targets the new function, and let at least one real scheduled
   run pass cleanly.
4. Only after verification passes, delete the old stack:
   `aws cloudformation delete-stack --stack-name drs-lag-monitor`.
5. Separately, consider deleting the old artifact bucket
   `drs-lambda-<repo-owner>-9915` (it was created by the old CI outside of the
   stack, so deleting the stack doesn't remove it).
6. In a follow-up commit, delete `drs-lag-monitor.yaml` and `lambda.py`.

Until step 4 is done, both the old and new stacks exist in the account
simultaneously — this is intentional, not a leftover.
