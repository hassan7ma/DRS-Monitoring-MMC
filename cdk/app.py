import os

import aws_cdk as cdk

from stacks.drs_monitor_stack import DrsMonitorStack

app = cdk.App()

# Config passed via -c flags at synth/deploy time:
#   cdk deploy -c alert_email=ops@customer.com -c lag_threshold=120 -c drs_regions=""
alert_email = app.node.try_get_context("alert_email")
if not alert_email:
    raise ValueError("Context 'alert_email' is required (-c alert_email=...)")

lag_threshold = int(app.node.try_get_context("lag_threshold") or 120)
drs_regions = app.node.try_get_context("drs_regions") or ""

DrsMonitorStack(
    app,
    "DrsMonitorStack",
    alert_email=alert_email,
    lag_threshold_seconds=lag_threshold,
    drs_regions=drs_regions,
    env=cdk.Environment(
        account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION"),
    ),
)

app.synth()
