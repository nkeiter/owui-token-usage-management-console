#!/usr/bin/env python3
"""Manage admin users (Cognito user pool) of the token usage management console.

Developed by Nathan I Keiter, Gettysburg College. Licensed under the MIT License.

Reads config.json (or --config / CONFIG_FILE) for the AWS profile and the environment's region, then
finds the user pool from the deployed CloudFormation stack output. Uses the AWS CLI, so no extra
Python packages are required.

Examples:
  ./manage_admins.py list   --env dev
  ./manage_admins.py add    --env dev  someone@example.edu
  ./manage_admins.py renew  --env prod someone@example.edu
  ./manage_admins.py remove --env prod someone@example.edu
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

STACK_PREFIX = "owui-token-usage-management-console"
EMAIL_RE = re.compile(r"^[^@\s,;<>\"']{1,64}@[^@\s,;<>\"']{1,255}\.[^@\s,;<>\"']{2,63}$")


def fail(message, code=1):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def load_settings(config_path, env_name):
    path = Path(config_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    if not path.is_file():
        fail(f"config file not found: {path} (copy config.example.json to config.json)", 2)
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    environment = config.get("environments", {}).get(env_name)
    if environment is None:
        fail(f"environment '{env_name}' is not defined in {path}", 2)
    return {
        "profile": os.environ.get("AWS_PROFILE") or config.get("aws_profile") or "",
        "region": os.environ.get("AWS_REGION") or environment.get("aws_region") or "",
        "user_pool_id": os.environ.get("USER_POOL_ID") or environment.get("user_pool_id") or "",
        "stack": f"{STACK_PREFIX}-{env_name}",
    }


def aws(settings, *args, check=True):
    command = ["aws", *args, "--region", settings["region"], "--output", "json"]
    if settings["profile"]:
        command += ["--profile", settings["profile"]]
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode != 0:
        fail(result.stderr.strip() or "AWS CLI command failed")
    return result


def resolve_pool(settings):
    if settings["user_pool_id"]:
        return settings["user_pool_id"]
    result = aws(
        settings, "cloudformation", "describe-stacks", "--stack-name", settings["stack"],
        "--query", "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue | [0]",
    )
    pool = json.loads(result.stdout)
    if not pool:
        fail(f"UserPoolId output not found on stack {settings['stack']}; has the console been deployed?")
    return pool


def get_user(settings, pool, email):
    result = aws(
        settings, "cognito-idp", "admin-get-user", "--user-pool-id", pool, "--username", email,
        check=False,
    )
    if result.returncode == 0:
        return json.loads(result.stdout)
    if "UserNotFoundException" in result.stderr:
        return None
    fail(result.stderr.strip())


def valid_email(email):
    if not EMAIL_RE.match(email):
        fail(f"'{email}' is not a valid e-mail address", 2)
    return email.lower()


def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def cmd_list(settings, pool, _args):
    result = aws(settings, "cognito-idp", "list-users", "--user-pool-id", pool)
    users = json.loads(result.stdout).get("Users", [])
    if not users:
        print("No admin users.")
        return
    print(f"{'EMAIL':<45} {'STATUS':<22} ENABLED")
    for user in sorted(users, key=lambda u: u["Username"]):
        attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
        print(f"{attrs.get('email', user['Username']):<45} {user['UserStatus']:<22} {user['Enabled']}")


def cmd_add(settings, pool, args):
    email = valid_email(args.email)
    if get_user(settings, pool, email):
        fail(f"{email} already exists; use 'renew' to send a new temporary password.")
    aws(
        settings, "cognito-idp", "admin-create-user", "--user-pool-id", pool, "--username", email,
        "--user-attributes", f"Name=email,Value={email}", "Name=email_verified,Value=true",
        "--desired-delivery-mediums", "EMAIL",
    )
    print(f"Created {email}. A temporary password was e-mailed; they must choose a new one at first sign-in.")


def cmd_renew(settings, pool, args):
    email = valid_email(args.email)
    user = get_user(settings, pool, email)
    if not user:
        fail(f"{email} does not exist; use 'add' first.")
    if not user.get("Enabled", True):
        fail(f"{email} is disabled; re-enable it in Cognito before renewing.")
    if user["UserStatus"] == "FORCE_CHANGE_PASSWORD":
        # Never signed in (or the temporary password expired): issue and e-mail a fresh temporary password.
        aws(
            settings, "cognito-idp", "admin-create-user", "--user-pool-id", pool, "--username", email,
            "--message-action", "RESEND", "--desired-delivery-mediums", "EMAIL",
        )
        print(f"A new temporary password was e-mailed to {email} (valid for the pool's configured period).")
        return
    # Cognito cannot e-mail a temporary password to a user who has already set one. Forcing a reset
    # invalidates the current password and e-mails a verification code for choosing a new one.
    if not confirm(
        f"{email} has already signed in (status {user['UserStatus']}). This invalidates their current "
        "password and e-mails a reset code. Continue?", args.yes,
    ):
        fail("Cancelled.", 3)
    aws(settings, "cognito-idp", "admin-reset-user-password", "--user-pool-id", pool, "--username", email)
    print(f"Password reset started for {email}; they will receive a code by e-mail and choose a new password at sign-in.")


def cmd_remove(settings, pool, args):
    email = valid_email(args.email)
    if not get_user(settings, pool, email):
        fail(f"{email} does not exist.")
    action = "admin-disable-user" if args.disable else "admin-delete-user"
    if not confirm(f"{'Disable' if args.disable else 'Permanently delete'} {email}?", args.yes):
        fail("Cancelled.", 3)
    aws(settings, "cognito-idp", action, "--user-pool-id", pool, "--username", email)
    print(f"{'Disabled' if args.disable else 'Deleted'} {email}.")


def main():
    parser = argparse.ArgumentParser(description="Manage console admin users (Cognito).")
    parser.add_argument("--config", default=os.environ.get("CONFIG_FILE", "config.json"), help="config file (default: config.json)")
    parser.add_argument("--env", default=os.environ.get("ENV_NAME", "dev"), choices=["dev", "prod"], help="environment (default: dev)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list admin users").set_defaults(func=cmd_list)
    for name, func, text in (
        ("add", cmd_add, "create an admin and e-mail a temporary password"),
        ("renew", cmd_renew, "e-mail a new temporary password (or start a password reset)"),
        ("remove", cmd_remove, "delete (or --disable) an admin"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("email")
        if name == "renew":
            p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
        if name == "remove":
            p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
            p.add_argument("--disable", action="store_true", help="disable instead of deleting")
        p.set_defaults(func=func)
    args = parser.parse_args()

    if shutil.which("aws") is None:
        fail("the AWS CLI is not installed or not on PATH", 2)
    settings = load_settings(args.config, args.env)
    if not settings["region"]:
        fail("no aws_region configured for this environment", 2)
    pool = resolve_pool(settings)
    args.func(settings, pool, args)


if __name__ == "__main__":
    main()
