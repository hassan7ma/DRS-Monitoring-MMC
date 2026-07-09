import os

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_sqs as sqs,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_events as events,
    aws_events_targets as targets,
)
from constructs import Construct

LAMBDA_ASSET_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")


class DrsMonitorStack(Stack):

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        alert_email: str,
        lag_threshold_seconds: int = 120,
        drs_regions: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------
        # Dead Letter Queue
        # ------------------------------------------------------------
        dlq = sqs.Queue(
            self, "LambdaDLQ",
            queue_name=f"drs-monitor-dlq-{self.account}",
            retention_period=Duration.days(14),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # KMS encryption was deliberately dropped from this project (see git
        # history "without KMS") to avoid the extra cost/complexity of a
        # customer-managed key for a low-sensitivity monitoring queue.
        dlq.node.default_child.add_metadata("checkov", {
            "skip": [{
                "id": "CKV_AWS_27",
                "comment": "KMS encryption intentionally not used for this low-sensitivity DLQ",
            }],
        })

        # ------------------------------------------------------------
        # SNS Topic + Email Subscription
        # ------------------------------------------------------------
        topic = sns.Topic(
            self, "AlertTopic",
            topic_name=f"drs-monitor-topic-{self.account}",
        )

        topic.node.default_child.add_metadata("checkov", {
            "skip": [{
                "id": "CKV_AWS_26",
                "comment": "KMS encryption intentionally not used for this low-sensitivity alert topic",
            }],
        })

        topic.add_subscription(subscriptions.EmailSubscription(alert_email))

        # ------------------------------------------------------------
        # Lambda Function
        # ------------------------------------------------------------
        role = iam.Role(
            self, "LambdaExecutionRole",
            role_name=f"drs-monitor-lambda-role-{self.account}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        role.add_to_policy(iam.PolicyStatement(
            sid="DRSRead",
            actions=["drs:DescribeSourceServers"],
            resources=["*"],
        ))

        role.add_to_policy(iam.PolicyStatement(
            sid="EC2Read",
            actions=["ec2:DescribeInstances"],
            resources=["*"],
        ))

        fn = lambda_.Function(
            self, "DRSLagChecker",
            function_name=f"drs-monitor-lambda-{self.account}",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.lambda_handler",
            code=lambda_.Code.from_asset(LAMBDA_ASSET_DIR),
            memory_size=128,
            timeout=Duration.seconds(120),
            reserved_concurrent_executions=2,
            role=role,
            dead_letter_queue=dlq,
            environment={
                "LAG_THRESHOLD_SECONDS": str(lag_threshold_seconds),
                "SNS_TOPIC_ARN": topic.topic_arn,
                "DRS_REGIONS": drs_regions,
            },
        )

        topic.grant_publish(fn)
        # Note: passing dead_letter_queue= above already grants the Lambda
        # role sqs:SendMessage on the DLQ - no separate grant needed.

        fn.node.default_child.add_metadata("checkov", {
            "skip": [
                {
                    "id": "CKV_AWS_117",
                    "comment": "Lambda only accesses public AWS APIs (DRS, EC2, SNS) - VPC not required",
                },
                {
                    "id": "CKV_AWS_173",
                    "comment": "KMS encryption intentionally not used for Lambda env vars (no secrets stored there)",
                },
            ],
        })

        # ------------------------------------------------------------
        # EventBridge Rule - every 8 hours
        # ------------------------------------------------------------
        rule = events.Rule(
            self, "DRSMonitorRule",
            rule_name=f"drs-monitor-rule-{self.account}",
            schedule=events.Schedule.rate(Duration.hours(8)),
            enabled=True,
        )

        rule.add_target(targets.LambdaFunction(fn))

        # ------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------
        CfnOutput(self, "LambdaName", value=fn.function_name)
        CfnOutput(self, "EventRuleName", value=rule.rule_name)
        CfnOutput(self, "DLQName", value=dlq.queue_name)
        CfnOutput(self, "SNSTopic", value=topic.topic_arn)
