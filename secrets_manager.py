###

import os
import re
import json
import logging
from typing import Dict, Any, Optional

import boto3
from botocore.exceptions import ClientError

try:
    # Available inside SageMaker notebooks / Studio
    from sagemaker.session import get_execution_role as _sm_get_execution_role
except Exception:  # pragma: no cover
    _sm_get_execution_role = None

log = logging.getLogger(__name__)


# ------------------------------ Role helpers ------------------------------- #

def get_role_arn() -> str:
    """Prefer SageMaker execution role when available, otherwise STS caller."""
    if _sm_get_execution_role is not None:
        try:
            role = _sm_get_execution_role()
            if role:
                log.info("Got execution role from SageMaker: %s", role)
                return role
        except Exception as e:  # pragma: no cover
            log.debug("SageMaker get_execution_role failed: %s", e)

    sts = boto3.client("sts")
    ident = sts.get_caller_identity()
    arn = ident["Arn"]  # arn:aws:sts::<acct>:assumed-role/<role-name>/<session> OR role ARN directly
    m = re.search(r":assumed-role/([^/]+)/", arn)
    if m:
        role_name = m.group(1)
        role_arn = f"arn:aws:iam::{ident['Account']}:role/{role_name}"
        log.info("Derived role ARN from STS: %s", role_arn)
        return role_arn
    return arn


def _parse_team_env_from_role(role_arn: str) -> Dict[str, Optional[str]]:
    """
    Extract team/env from typical role names.

    Examples:
      apps-globaltech-dev-app-ondemand-ecs-task  -> team='globaltech', env='dev'
      apps-pcoe-dev-app-ondemand-ecs-execution   -> team='pcoe',       env='dev'
      .../exec-domain-role-globaltech            -> team='globaltech'
      .../exec-role-commercial                   -> team='commercial'
    """
    role_name = role_arn.split("/")[-1]

    # Most robust for your ECS roles
    if role_name.startswith("apps-"):
        parts = role_name.split("-")
        if len(parts) >= 3:
            # apps-<team>-<env>-...
            return {"team": parts[1], "env": parts[2]}
        if len(parts) >= 2:
            return {"team": parts[1], "env": None}

    # SageMaker/studio style role names
    m = re.search(r"exec-domain-role-([a-z0-9\-]+)", role_name, re.I)
    if m:
        return {"team": m.group(1), "env": None}

    m = re.search(r"exec-role-([a-z0-9\-]+)", role_name, re.I)
    if m:
        return {"team": m.group(1), "env": None}

    # Fallback: TEAM_NAME env
    team = os.getenv("TEAM_NAME")
    return {"team": team, "env": None}


def _is_domain_exec_role(role_arn: str) -> bool:
    role_name = role_arn.split("/")[-1]
    return "exec-domain-role-" in role_name


# ---------------------------- Username resolver ---------------------------- #

def _resolve_username(explicit: Optional[str] = None) -> str:
    """
    Determine username for individual secrets.
    Precedence:
      1) explicit parameter
      2) SHINYPROXY_USERNAME env (on-demand seats)
      3) HTTP_X_SP_USERNAME / X_SP_USERNAME env (servers exporting headers)
    If none found, raise with a helpful message.
    """
    if explicit:
        return explicit

    for k in ("SHINYPROXY_USERNAME", "HTTP_X_SP_USERNAME", "X_SP_USERNAME"):
        v = os.getenv(k)
        if v:
            return v

    raise ValueError(
        "Username not found. Pass `username=...` (use X-SP-USERNAME request header "
        "from ShinyProxy) or set SHINYPROXY_USERNAME env (on-demand seats)."
    )


# -------------------------- Path & CRUD operations ------------------------- #

def _sanitize(seg: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-\.@]", "-", seg.strip())


def _secret_path(shared: bool, secret_secret: str = "credentials", username: Optional[str] = None) -> str:
    """
    Build the secret path that stores the JSON map of connections.
      Shared:     /aap/<team>/shared/<secret_secret>
      Individual: /aap/<team>/<username>/<secret_secret>

    NOTE: to preserve existing SageMaker Studio behaviour, if we detect a role
    name `.../exec-domain-role-<team>`, we FORCE `shared=True` regardless of the
    value passed in by the caller.
    """
    role_arn = get_role_arn()

    # --- Preserve Studio domain behaviour: force shared=True ----------------
    if _is_domain_exec_role(role_arn):
        shared = True
    # -----------------------------------------------------------------------

    meta = _parse_team_env_from_role(role_arn)
    team = meta["team"] or os.getenv("TEAM_NAME")

    if not team:
        raise ValueError("TEAM_NAME could not be derived from role name; set TEAM_NAME env.")

    team = _sanitize(team)
    secret_secret = _sanitize(secret_secret)

    if shared:
        return f"/aap/{team}/shared/{secret_secret}"

    user = _resolve_username(username)
    user = _sanitize(user.replace("@", "_at_"))
    return f"/aap/{team}/{user}/{secret_secret}"


def _sm():
    return boto3.client("secretsmanager")


def _secret_of_secrets(shared: bool, username: Optional[str] = None) -> Dict[str, Any]:
    """
    Load the JSON map from the scope's "credentials" secret.
    Returns {} if the secret does not exist yet.
    """
    secret_id = _secret_path(shared, "credentials", username)
    try:
        resp = _sm().get_secret_value(SecretId=secret_id)
        data = resp.get("SecretString") or "{}"
        # Permit both JSON and Python dict-string legacy formats
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            import ast
            return ast.literal_eval(data)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("ResourceNotFoundException", "DecryptionFailureException"):
            return {}
        raise


def list_connections(shared: bool = False, username: Optional[str] = None) -> Dict[str, Any]:
    """Return the entire map for the scope."""
    return _secret_of_secrets(shared, username)


def get_connection(name: str, shared: bool = False, username: Optional[str] = None):
    """Return one entry by name from the scope; raises KeyError if missing."""
    conns = _secret_of_secrets(shared, username)
    return conns[name]


def put_connection(name: str, secret_value: Any, shared: bool = False, username: Optional[str] = None):
    """
    Upsert an entry in the scope map and save it back to Secrets Manager.
    Accepts dicts or strings; dicts are JSON-serialized.
    """
    conns = _secret_of_secrets(shared, username)
    conns[name] = secret_value
    secret_id = _secret_path(shared, "credentials", username)
    payload = json.dumps(conns)
    return _sm().put_secret_value(SecretId=secret_id, SecretString=payload)


def delete_connection(name: str, shared: bool = False, username: Optional[str] = None):
    """
    Remove an entry from the scope map. If the map becomes empty, it is still
    kept as an empty JSON object.
    """
    conns = _secret_of_secrets(shared, username)
    conns.pop(name, None)
    secret_id = _secret_path(shared, "credentials", username)
    payload = json.dumps(conns)
    return _sm().put_secret_value(SecretId=secret_id, SecretString=payload)
