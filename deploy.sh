#!/usr/bin/env bash
# Build and deploy the console using config.json (or CONFIG_FILE).
set -euo pipefail
cd "$(dirname "$0")"

ENV_NAME="${ENV_NAME:-dev}"
CONFIG_FILE="${CONFIG_FILE:-config.json}"
[[ "$ENV_NAME" == "dev" || "$ENV_NAME" == "prod" ]] || {
  echo "ERROR: ENV_NAME must be 'dev' or 'prod'." >&2
  exit 2
}
[[ -f "$CONFIG_FILE" ]] || {
  echo "ERROR: config file not found: $CONFIG_FILE (copy config.example.json to config.json and edit it)." >&2
  exit 2
}

config_value() {
  python3 - "$CONFIG_FILE" "$ENV_NAME" "$1" "${2:-}" <<'PY'
import json
import sys

filename, environment, key, fallback = sys.argv[1:]
with open(filename, encoding="utf-8") as config_file:
    config = json.load(config_file)
environment_values = config.get("environments", {}).get(environment, {})
value = environment_values.get(key, config.get(key, fallback))
if isinstance(value, list):
    value = ",".join(str(item) for item in value)
elif value is None:
    value = fallback
print(value)
PY
}

CONFIG_AWS_PROFILE="$(config_value aws_profile)"
AWS_PROFILE="${AWS_PROFILE:-$CONFIG_AWS_PROFILE}"
if [[ -n "$AWS_PROFILE" ]]; then export AWS_PROFILE; fi

REGION="${AWS_REGION:-$(config_value aws_region us-west-2)}"
STACK="owui-token-usage-management-console-${ENV_NAME}"
VPC_ID="${VPC_ID:-$(config_value vpc_id)}"
SUBNETS="${SUBNETS:-$(config_value private_subnet_ids)}"
DATABASE_SG="${DATABASE_SECURITY_GROUP_ID:-${AURORA_SG:-$(config_value database_security_group_id)}}"
SECRET_ID="${SECRET_ID:-$(config_value secret_id)}"
SECRET_DSN_KEY="${SECRET_DSN_KEY:-$(config_value secret_dsn_key DATABASE_URL)}"
OWUI_URL="${OWUI_URL:-$(config_value open_web_ui_url)}"
CUSTOM_DOMAIN_NAME="${CUSTOM_DOMAIN_NAME:-$(config_value custom_domain_name)}"
CERTIFICATE_ARN="${CERTIFICATE_ARN:-$(config_value certificate_arn)}"
COGNITO_DOMAIN_PREFIX="${COGNITO_DOMAIN_PREFIX:-$(config_value cognito_domain_prefix)}"
ARTIFACT_BUCKET_PREFIX="${ARTIFACT_BUCKET_PREFIX:-$(config_value artifact_bucket_prefix owui-usage-console-artifacts)}"
FILTER_FILE="${FILTER_FILE:-$(config_value filter_file)}"
USAGE_TIMEZONE="${USAGE_TIMEZONE:-$(config_value usage_timezone UTC)}"
USAGE_TIER_PREFIX="${USAGE_TIER_PREFIX:-$(config_value usage_tier_prefix token-limit-)}"
USAGE_BUILTIN_TIERS="${USAGE_BUILTIN_TIERS:-$(config_value usage_builtin_tiers token-limit-enterprise,token-limit-pro,token-limit-standard,token-limit-test)}"
USAGE_DEFAULT_TIER="${USAGE_DEFAULT_TIER:-$(config_value usage_default_tier token-limit-standard)}"
USAGE_DEFAULT_DAILY_TOKENS="${USAGE_DEFAULT_DAILY_TOKENS:-$(config_value usage_default_daily_tokens 500000)}"
USAGE_DEFAULT_MONTHLY_TOKENS="${USAGE_DEFAULT_MONTHLY_TOKENS:-$(config_value usage_default_monthly_tokens 5000000)}"

if [[ -z "$VPC_ID" || -z "$SUBNETS" || -z "$DATABASE_SG" || -z "$SECRET_ID" ]]; then
  echo "ERROR: VPC_ID, SUBNETS, DATABASE_SECURITY_GROUP_ID, and SECRET_ID must be set in $CONFIG_FILE or the environment." >&2
  exit 2
fi
if [[ -z "$FILTER_FILE" ]]; then
  echo "ERROR: filter_file must be set in $CONFIG_FILE or FILTER_FILE must be set in the environment." >&2
  exit 2
fi

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${ARTIFACT_BUCKET_NAME:-${ARTIFACT_BUCKET_PREFIX}-${ACCOUNT}-${REGION}}"
DOMAIN_PREFIX="${COGNITO_DOMAIN_PREFIX:-owui-usage-console-${ENV_NAME}-${ACCOUNT}}"

rm -rf build && mkdir build
# Lambda runs on arm64 Linux: fetch matching prebuilt wheels regardless of the host OS.
pip install --quiet --target build --platform manylinux2014_aarch64 \
  --python-version 3.12 --implementation cp --only-binary=:all: "psycopg[binary]>=3.2,<4" typing_extensions
cp -R src/app.py src/static build/

# Bundle the tier-resolution SQL (resolve_user_tier, resolve_user_limits, get_user_usage_status) straight
# from the filter source so the console's copy cannot drift from what the filter enforces.
[ -f "$FILTER_FILE" ] || { echo "ERROR: filter source not found at $FILTER_FILE (set FILTER_FILE)"; exit 1; }
python3 - "$FILTER_FILE" build/tier_function.sql "$USAGE_TIER_PREFIX" "$USAGE_BUILTIN_TIERS" \
  "$USAGE_DEFAULT_TIER" "$USAGE_TIMEZONE" "$USAGE_DEFAULT_DAILY_TOKENS" "$USAGE_DEFAULT_MONTHLY_TOKENS" <<'PY'
import re, sys, textwrap
src_file, out_file, prefix, builtins, default_tier, timezone, daily, monthly = sys.argv[1:]
src = open(src_file, encoding="utf-8").read()
m = re.search(r"-- BEGIN tier-functions\n(.*?)\n\s*-- END tier-functions", src, re.S)
if not m:
    sys.exit("BEGIN/END tier-functions markers not found in " + src_file)
sql = textwrap.dedent(" " * 8 + m.group(1))

def lit(value):
    return "'" + value.replace("'", "''") + "'"

# The filter builds this block as an f-string; fill the same placeholders from the console settings.
replacements = {
    "{tier_names_sql}": ", ".join(lit(t) for t in builtins.split(",") if t),
    "{tier_group_prefix_sql}": lit(prefix),
    "{default_group_sql}": lit(default_tier),
    "{timezone_sql}": lit(timezone),
    "{int(default_tier['daily_token_limit'])}": str(int(daily)),
    "{int(default_tier['monthly_token_limit'])}": str(int(monthly)),
}
for placeholder, value in replacements.items():
    sql = sql.replace(placeholder, value)
leftover = re.findall(r"\{[A-Za-z_(][^}]*\}", sql)
if leftover:
    sys.exit("Unrendered placeholders in tier functions: " + ", ".join(sorted(set(leftover))))
open(out_file, "w", encoding="utf-8").write(sql + "\n")
PY
echo "Bundled tier functions from $FILTER_FILE"

aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null || {
  # us-east-1 is the one region that must not be given a LocationConstraint
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION"
  fi
}
aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$BUCKET" --region "$REGION" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws cloudformation package --template-file template.yaml --s3-bucket "$BUCKET" \
  --output-template-file packaged.yaml --region "$REGION"

aws cloudformation deploy --template-file packaged.yaml --stack-name "$STACK" \
  --region "$REGION" --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides EnvironmentName="$ENV_NAME" VpcId="$VPC_ID" \
    PrivateSubnetIds="$SUBNETS" DatabaseSecurityGroupId="$DATABASE_SG" \
    SecretId="$SECRET_ID" SecretDsnKey="$SECRET_DSN_KEY" \
    CognitoDomainPrefix="$DOMAIN_PREFIX" OpenWebUiUrl="$OWUI_URL" \
    CustomDomainName="$CUSTOM_DOMAIN_NAME" CertificateArn="$CERTIFICATE_ARN" \
    UsageTimezone="$USAGE_TIMEZONE" UsageTierPrefix="$USAGE_TIER_PREFIX" \
    UsageBuiltinTiers="$USAGE_BUILTIN_TIERS" UsageDefaultTier="$USAGE_DEFAULT_TIER" \
    UsageDefaultDailyTokens="$USAGE_DEFAULT_DAILY_TOKENS" \
    UsageDefaultMonthlyTokens="$USAGE_DEFAULT_MONTHLY_TOKENS"

aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query 'Stacks[0].Outputs' --output table
