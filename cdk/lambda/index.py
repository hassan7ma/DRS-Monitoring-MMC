import os
import re
import logging
import boto3
from collections import defaultdict

logger = logging.getLogger()
logger.setLevel(logging.INFO)

LAG_THRESHOLD = int(os.environ["LAG_THRESHOLD_SECONDS"])
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
DRS_REGIONS = os.environ.get("DRS_REGIONS", "")

sns = boto3.client("sns")

def get_regions():
  if not DRS_REGIONS.strip():
      return [os.environ["AWS_REGION"]]
  return [r.strip() for r in DRS_REGIONS.split(",")]

def get_ec2_name(instance_id, region):
  if not instance_id:
      return "unknown"

  try:
      ec2 = boto3.client("ec2", region_name=region)
      response = ec2.describe_instances(InstanceIds=[instance_id])

      for r in response.get("Reservations", []):
          for inst in r.get("Instances", []):
              for tag in inst.get("Tags", []):
                  if tag.get("Key") == "Name":
                      return tag.get("Value")

  except Exception as e:
      logger.warning(f"EC2 lookup failed in {region}: {e}")

  return "unknown"

def parse_iso_duration(duration):

  match = re.match(
      r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
      duration
  )

  if not match:
      return 0

  h, m, s = match.groups(default="0")

  return int(h)*3600 + int(m)*60 + int(s)

def lambda_handler(event, context):

  logger.info("DRS Monitor Started")

  regions = get_regions()

  logger.info(f"Checking regions: {regions}")

  problematic = []
  total = 0

  for region in regions:

      logger.info(f"Scanning region: {region}")

      drs = boto3.client("drs", region_name=region)
      paginator = drs.get_paginator("describe_source_servers")

      for page in paginator.paginate():

          for server in page.get("items", []):

              total += 1

              sid = server.get("sourceServerID")

              src_id = (
                  server.get("sourceProperties", {})
                  .get("identificationHints", {})
                  .get("awsInstanceID")
              )

              ec2_name = get_ec2_name(src_id, region)

              hostname = (
                  server.get("sourceProperties", {})
                  .get("identificationHints", {})
                  .get("hostname", "unknown")
              )

              lag_str = (
                  server.get("dataReplicationInfo", {})
                  .get("lagDuration")
              )

              if not lag_str:
                  continue

              lag = parse_iso_duration(lag_str)

              if lag > LAG_THRESHOLD:

                  problematic.append({
                      "id": sid,
                      "name": ec2_name if ec2_name != "unknown" else hostname,
                      "lag": lag,
                      "region": region
                  })

  logger.info(f"Checked {total} servers")

  if problematic:
      publish_alert(problematic, total)

  return {
      "status": "completed",
      "checked": total,
      "violations": len(problematic)
  }

def build_tables(servers):

  grouped = defaultdict(list)

  for s in servers:
      grouped[s["region"]].append(s)

  message = ""
  message += "AWS DRS Replication Lag Alert\n"
  message += "=================================\n\n"

  for region, items in grouped.items():

      message += f"Region: {region}\n"
      message += "-" * 60 + "\n"
      message += f"{'Server':25} {'ID':20} {'Lag(s)':10}\n"
      message += "-" * 60 + "\n"

      for s in items:

          name = s["name"][:24]
          sid = s["id"][:19]

          message += (
              f"{name:25} {sid:20} {s['lag']:10}\n"
          )

      message += "-" * 60 + "\n\n"

  return message

def publish_alert(servers, total):

  logger.info("Publishing SNS alert")

  tables = build_tables(servers)

  message = (
      f"DRS Replication Lag Detected\n\n"
      f"Total Servers Checked: {total}\n"
      f"Servers Exceeding Threshold: {len(servers)}\n\n"
      f"{tables}"
  )

  sns.publish(
      TopicArn=SNS_TOPIC_ARN,
      Subject="AWS DRS Replication Lag Alert",
      Message=message
  )

  logger.info("SNS message sent")
