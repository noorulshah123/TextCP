import os
from datetime import datetime, timezone, timedelta
import boto3

ecs = boto3.client('ecs')

MAX_AGE_HOURS = int(os.environ.get('MAX_TASK_AGE_HOURS', '48'))

def _env_map(container_def):
    return {e['name']: e['value'] for e in container_def.get('environment', [])}

def _is_preinit_task(task_desc, task_def):
    """
    Decide if a task is a pre-initialized app.
    We check in this order:
      1) task tags (if present)
      2) dockerLabels on container definition
      3) PRE_INIT_MODE env var
    Return True for preinit, False otherwise.
    """
    # 1) task tags
    for t in task_desc.get('tags', []):
        if t.get('key') == 'sp.mode' and t.get('value') == 'preinit':
            return True

    # 2) docker labels
    for c in task_def.get('containerDefinitions', []):
        labels = c.get('dockerLabels', {}) or {}
        mode = (labels.get('sp.mode') or labels.get('SP_MODE') or '').lower()
        if mode == 'preinit':
            return True

    # 3) env var
    for c in task_def.get('containerDefinitions', []):
        env = _env_map(c)
        if env.get('PRE_INIT_MODE', '').lower() == 'true':
            return True

    return False

def _is_ondemand_task(task_desc, task_def):
    # By definition: ondemand if it's not preinit
    return not _is_preinit_task(task_desc, task_def)

def lambda_handler(event, context):
    clusters = []
    resp = ecs.list_clusters()
    clusters.extend(resp.get('clusterArns', []))
    while resp.get('nextToken'):
        resp = ecs.list_clusters(nextToken=resp['nextToken'])
        clusters.extend(resp.get('clusterArns', []))

    for cluster in clusters:
        # Get services to derive latest deployment time (as in your code)
        svc_arns = []
        r = ecs.list_services(cluster=cluster)
        svc_arns.extend(r.get('serviceArns', []))
        while r.get('nextToken'):
            r = ecs.list_services(cluster=cluster, nextToken=r['nextToken'])
            svc_arns.extend(r.get('serviceArns', []))

        deployments = []
        if svc_arns:
            d = ecs.describe_services(cluster=cluster, services=svc_arns)
            for svc in d.get('services', []):
                for dep in svc.get('deployments', []):
                    deployments.append(dep.get('updatedAt'))
        last_deploy_time = max(deployments) if deployments else datetime.min.replace(tzinfo=timezone.utc)

        # List currently running tasks
        task_arns = []
        r = ecs.list_tasks(cluster=cluster, desiredStatus='RUNNING')
        task_arns.extend(r.get('taskArns', []))
        while r.get('nextToken'):
            r = ecs.list_tasks(cluster=cluster, desiredStatus='RUNNING', nextToken=r['nextToken'])
            task_arns.extend(r.get('taskArns', []))
        if not task_arns:
            continue

        # Describe tasks (include tags if you use them)
        tdesc = ecs.describe_tasks(cluster=cluster, tasks=task_arns, include=['TAGS'])
        for task in tdesc.get('tasks', []):
            task_def_arn = task['taskDefinitionArn']
            tdef = ecs.describe_task_definition(taskDefinition=task_def_arn)['taskDefinition']

            # === Filter: only OnDemand ===
            if not _is_ondemand_task(task, tdef):
                # skip pre-initialized apps
                continue

            # Age check
            started_at = task.get('startedAt')
            if not started_at:
                continue  # still provisioning, skip

            age = datetime.now(timezone.utc) - started_at
            if age < timedelta(hours=MAX_AGE_HOURS):
                continue

            # Safety: don't kill tasks from the latest deployment (optional)
            if started_at >= last_deploy_time:
                continue

            # Optional additional safety: only stop tasks where container image contains your runtime
            container_defs = tdef.get('containerDefinitions', [])
            images = [c.get('image', '') for c in container_defs]
            if not any('app-runtime' in img or 'shiny' in img.lower() for img in images):
                continue

            print(f"Stopping OnDemand task: {task['taskArn']} | cluster: {cluster} | age: {age}")
            ecs.stop_task(
                cluster=cluster,
                task=task['taskArn'],
                reason='ShinyProxy OnDemand task exceeded max age and predates last deployment'
            )

    return {'statusCode': 200, 'body': 'OnDemand task cleanup complete.'}
