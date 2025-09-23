import os
from pathlib import Path
import subprocess
import click
import modules.gitlibs as gitlibs
import modules.helpers as helpers
import tempfile
import shutil
import json
import ipaddr
import modules.cloud_config as cloud_config

import pathlib
from typing import Optional
import json
import subprocess

def _load_json_maybe_plan(planfile: str) -> dict:
    p = pathlib.Path(planfile)
    try:
        head = p.read_bytes()[:2048].lstrip()
        if head.startswith(b"{"):
            return json.loads(p.read_text())
        rc = subprocess.run(["terraform", "show", "-json", str(p)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if rc.returncode == 0 and rc.stdout.strip().startswith("{"):
            return json.loads(rc.stdout)
        click.echo(click.style(
            "ERROR: Plan file isn't JSON and 'terraform show -json' failed.\n"
            "Convert first: terraform show -json tfplan > plan.json", fg="red", bold=True))
        raise SystemExit(1)
    except Exception as e:
        click.echo(click.style(f"ERROR reading plan file: {e}", fg="red", bold=True))
        raise SystemExit(1)

def _plandata_from_plan(plan_json: dict) -> dict:
    if "resource_changes" not in plan_json:
        click.echo(click.style("ERROR: missing 'resource_changes' in plan JSON.", fg="red", bold=True))
        raise SystemExit(1)
    return {"resource_changes": plan_json["resource_changes"]}

def _plandata_from_state(state_json: dict) -> dict:
    changes = []
    for r in state_json.get("resources", []):
        base = {
            "mode": r.get("mode", "managed"),
            "type": r.get("type"),
            "name": r.get("name"),
            "module_address": ("module." + r["module"]) if r.get("module") else None,
        }
        instances = r.get("instances") or [{}]
        for inst in instances:
            changes.append({
                "address": r.get("address"),
                **base,
                "index": inst.get("index_key"),
                "change": {
                    "actions": ["no-op"],
                    "after": inst.get("attributes", {}) or {},
                    "after_unknown": {},
                    "after_sensitive": {}
                }
            })
    return {"resource_changes": changes}

def tf_from_offline(planfile: Optional[str] = None, statefile: Optional[str] = None) -> dict:
    if not (planfile or statefile):
        click.echo(click.style("ERROR: offline mode requires --planfile or --statefile", fg="red", bold=True))
        raise SystemExit(1)

    if planfile:
        plan_json = _load_json_maybe_plan(planfile)
        plandata = _plandata_from_plan(plan_json)
    else:
        try:
            state_json = json.loads(open(statefile, "r").read())
        except Exception as e:
            click.echo(click.style(f"ERROR reading state file: {e}", fg="red", bold=True))
            raise SystemExit(1)
        plandata = _plandata_from_state(state_json)

    # Mirror normal pipeline: seed tfdata → make nodes
    tfdata = dict()
    tfdata["workdir"] = os.getcwd()
    tfdata["codepath"] = []
    tfdata["all_variable"] = {}
    tfdata["all_module"] = {}

    tfdata = make_tf_data(tfdata, plandata, graphdata={"objects": [], "edges": []}, codepath=[])
    tfdata = setup_graph(tfdata)
    return tfdata


# import io
# import pathlib
# import json
# import subprocess

# def _load_json_maybe_plan(planfile: str) -> dict:
#     """Load a plan as JSON. If it's a binary .plan, attempt 'terraform show -json'."""
#     p = pathlib.Path(planfile)
#     data = None
#     try:
#         # quick check: is it already JSON?
#         txt = open(p, "rb").read(2048)
#         if txt.strip().startswith(b"{"):
#             data = json.loads(open(p, "r").read())
#         else:
#             # try terraform show -json (doesn't need cloud auth)
#             rc = subprocess.run(["terraform", "show", "-json", str(p)],
#                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, text=True)
#             if rc.returncode == 0 and rc.stdout.strip().startswith("{"):
#                 data = json.loads(rc.stdout)
#             else:
#                 click.echo(click.style(
#                     "\nERROR: Plan file isn't JSON and 'terraform show -json' failed. "
#                     "Please convert your plan with 'terraform show -json planfile > plan.json' and pass --planfile plan.json",
#                     fg="red", bold=True))
#                 exit()
#     except Exception as e:
#         click.echo(click.style(f"\nERROR: Could not read plan file: {e}", fg="red", bold=True))
#         exit()
#     return data

# def _plandata_from_state(state: dict) -> dict:
#     """
#     Convert state JSON into a pseudo 'resource_changes' list so the rest of TerraVision can reuse its pipeline.
#     We mark each resource as a no-op 'create' with attributes from state.
#     """
#     changes = []
#     for r in state.get("resources", []):
#         addr = r.get("address") or (("module."  r["module"]) if r.get("module") else "")  (r.get("type","") and f'{r["type"]}.{r.get("name","")}')  # best effort
#         for inst in r.get("instances", []) or [{}]:
#             idx = inst.get("index_key")
#             after = inst.get("attributes", {}) or {}
#             changes.append({
#                 "address": addr,
#                 "mode": "managed",
#                 "type": r.get("type"),
#                 "name": r.get("name"),
#                 "index": idx if idx is not None else None,
#                 "change": {
#                     "actions": ["no-op"],
#                     "after": after,
#                     "after_unknown": {},
#                     "after_sensitive": {}
#                 },
#                 "module_address": ("module."  r["module"]) if r.get("module") else None,
#             })
#     return {"resource_changes": changes}

# def _plandata_from_plan(plan: dict) -> dict:
#     """Just pass through the resource_changes list from 'terraform show -json' output."""
#     if "resource_changes" not in plan:
#         click.echo(click.style("\nERROR: The provided plan JSON doesn't have 'resource_changes'. Did you pass the correct file?", fg="red", bold=True))
#         exit()
#     return {"resource_changes": plan["resource_changes"]}

# def tf_from_offline(planfile: str = None, statefile: str = None) -> dict:
#     """
#     Build tfdata from a plan JSON (preferred) or a state JSON, skipping any terraform CLI calls.
#     Graph edges from 'terraform graph' are not used; we build nodes and let graphmaker add relationships.
#     """
#     if not (planfile or statefile):
#         click.echo(click.style("\nERROR: offline mode called without a plan or state file.", fg="red", bold=True))
#         exit()
#     tfdata = dict()
#     tfdata["workdir"] = os.getcwd()
#     tfdata["codepath"] = []  # unknown in offline mode
#     tfdata["all_variable"] = dict()
#     tfdata["all_module"] = dict()

#     if planfile:
#         plan = _load_json_maybe_plan(planfile)
#         plandata = _plandata_from_plan(plan)
#     else:
#         try:
#             state = json.loads(open(statefile, "r").read())
#         except Exception as e:
#             click.echo(click.style(f"\nERROR: Could not read state file: {e}", fg="red", bold=True)); exit()
#         plandata = _plandata_from_state(state)

#     # Seed the model exactly like normal flow
#     tfdata = make_tf_data(tfdata, plandata, graphdata={"objects": [], "edges": []}, codepath=[])
#     # Normally tf_makegraph() uses 'terraform graph' to add edges. We skip it:
#     tfdata = setup_graph(tfdata)  # creates nodes  meta_data, leaves connections empty
#     return tfdata

# Create Tempdir and Module Cache Directories
annotations = dict()
# basedir =  os.path.dirname(os.path.isfile("terravision"))
basedir = Path(os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
start_dir = Path.cwd()
temp_dir = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
os.environ["TF_DATA_DIR"] = temp_dir.name
abspath = os.path.abspath(__file__)
dname = os.path.dirname(abspath)
MODULE_DIR = str(Path(Path.home(), ".terravision", "module_cache"))
REVERSE_ARROW_LIST = cloud_config.AWS_REVERSE_ARROW_LIST


def tf_initplan(source: tuple, varfile: list, workspace: str):
    tfdata = dict()
    tfdata["codepath"] = list()
    tfdata["workdir"] = os.getcwd()
    for sourceloc in source:
        if os.path.isdir(sourceloc):
            os.chdir(sourceloc)
            codepath = sourceloc
        else:
            githubURL, subfolder, git_tag = gitlibs.get_clone_url(sourceloc)
            codepath = gitlibs.clone_files(sourceloc, temp_dir.name)
            ovpath = os.path.join(basedir, "override.tf")
            shutil.copy(ovpath, codepath)
            os.chdir(codepath)
            codepath = [codepath]
            if len(os.listdir()) == 0:
                click.echo(
                    click.style(
                        f"\n  ERROR: No files found to process.",
                        fg="red",
                        bold=True,
                    )
                )
                exit()
        returncode = os.system(f"terraform init --upgrade")
        if returncode > 0:
            click.echo(
                click.style(
                    f"\nERROR: Cannot perform terraform init using provided source. Check providers and backend config.",
                    fg="red",
                    bold=True,
                )
            )
            exit()
        if varfile:
            vfile = varfile[0]
            if not os.path.isabs(vfile):
                vfile = os.path.join(start_dir, vfile)

        click.echo(
            click.style(
                f"\nInitalising workspace: {workspace}\n", fg="white", bold=True
            )
        )
        # init workspace
        returncode = os.system(f"terraform workspace select {workspace}")
        if returncode:
            click.echo(
                click.style(
                    f"\nERROR: Invalid output from 'terraform workspace select {workspace}' command.",
                    fg="red",
                    bold=True,
                )
            )
            exit()

        click.echo(
            click.style(f"\nGenerating Terraform Plan..\n", fg="white", bold=True)
        )
        # Get Temporary directory paths for intermediary files
        tempdir = os.path.dirname(temp_dir.name)
        tfplan_path = os.path.join(tempdir, "tfplan.bin")
        if os.path.exists(tfplan_path):
            os.remove(tfplan_path)
        tfplan_json_path = os.path.join(tempdir, "tfplan.json")
        if os.path.exists(tfplan_json_path):
            os.remove(tfplan_json_path)
        tfgraph_path = os.path.join(tempdir, "tfgraph.dot")
        if os.path.exists(tfgraph_path):
            os.remove(tfgraph_path)
        tfgraph_json_path = os.path.join(tempdir, "tfgraph.json")
        if os.path.exists(tfgraph_json_path):
            os.remove(tfgraph_json_path)
        if varfile:
            returncode = os.system(
                f"terraform plan -refresh=false -var-file {vfile} -out {tfplan_path}"
            )
        else:
            returncode = os.system(f"terraform plan -refresh=false -out {tfplan_path}")
        click.echo(click.style(f"\nDecoding plan..\n", fg="white", bold=True))
        if (
            os.path.exists(tfplan_path)
            and os.system(f"terraform show -json {tfplan_path} > {tfplan_json_path}")
            == 0
        ):
            click.echo(click.style(f"\nAnalysing plan..\n", fg="white", bold=True))
            f = open(tfplan_json_path)
            plandata = json.load(f)
            returncode = os.system(f"terraform graph > {tfgraph_path}")
            tfdata["plandata"] = dict
            click.echo(
                click.style(
                    f"\nConverting TF Graph Connections..  (this may take a while)\n",
                    fg="white",
                    bold=True,
                )
            )
            if os.path.exists(tfgraph_path):
                returncode = os.system(
                    f"dot -Txdot_json -o {tfgraph_json_path} {tfgraph_path}"
                )
                f = open(tfgraph_json_path)
                graphdata = json.load(f)
            else:
                click.echo(
                    click.style(
                        f"\nERROR: Invalid output from 'terraform graph' command. Check your TF source files can generate a valid plan and graph",
                        fg="red",
                        bold=True,
                    )
                )
                exit()
        else:
            click.echo(
                click.style(
                    f"\nERROR: Invalid output from 'terraform plan' command. Try using the terraform CLI first to check source files have no errors.",
                    fg="red",
                    bold=True,
                )
            )
            exit()
        tfdata = make_tf_data(tfdata, plandata, graphdata, codepath)
    os.chdir(start_dir)
    return tfdata


def make_tf_data(tfdata: dict, plandata: dict, graphdata: dict, codepath: str) -> dict:
    tfdata["codepath"] = codepath
    if plandata.get("resource_changes"):
        tfdata["tf_resources_created"] = plandata["resource_changes"]
    else:
        click.echo(
            click.style(
                f"\nERROR: Invalid output from 'terraform plan' command. Try using the terraform CLI first to check source files have no errors.",
                fg="red",
                bold=True,
            )
        )
        exit()
    tfdata["tfgraph"] = graphdata
    return tfdata


def setup_graph(tfdata: dict):
    tfdata["graphdict"] = dict()
    tfdata["meta_data"] = dict()
    tfdata["all_output"] = dict()
    tfdata["node_list"] = list()
    tfdata["hidden"] = dict()
    tfdata["annotations"] = dict()
    # Make an initial dict with resources created and empty connections
    for object in tfdata["tf_resources_created"]:
        if object["mode"] == "managed":
            # Replace multi count notation
            # node = helpers.get_no_module_name(object["address"])
            node = str(object["address"])
            if "index" in object.keys():
                # node = object["type"] + "." + object["name"]
                if not isinstance(object["index"], int):
                    suffix = "[" + object["index"] + "]"
                else:
                    suffix = "~" + str(int(object.get("index")) + 1)
                node = node + suffix
            tfdata["graphdict"][node] = list()
            tfdata["node_list"].append(node)
            # Add metadata
            details = object["change"]["after"]
            details.update(object["change"]["after_unknown"])
            details.update(object["change"]["after_sensitive"])
            if "module." in object["address"]:
                modname = object["module_address"].split("module.")[1]
                details["module"] = modname
            tfdata["meta_data"][node] = details
    tfdata["node_list"] = list(dict.fromkeys(tfdata["node_list"]))
    return tfdata


def tf_makegraph(tfdata: dict):
    # Setup Initial graphdict
    tfdata = setup_graph(tfdata)
    # Make a lookup table of gvids mapping resources to ids
    gvid_table = list()
    for item in tfdata["tfgraph"]["objects"]:
        gvid = item["_gvid"]
        gvid_table.append("")
        gvid_table[gvid] = str(item.get("label"))
    # Populate connections list for each node in graphdict
    for node in dict(tfdata["graphdict"]):
        nodename = node.split("~")[0]
        if nodename in gvid_table:
            node_id = gvid_table.index(nodename)
        else:
            nodename = helpers.remove_brackets_and_numbers(nodename)
            node_id = gvid_table.index(nodename)
        if tfdata["tfgraph"].get("edges"):
            for connection in tfdata["tfgraph"]["edges"]:
                head = connection["head"]
                tail = connection["tail"]
                # Check that the connection is part of the nodes that will be created (exists in graphdict)
                if (
                    node_id == head
                    and len(
                        [
                            k
                            for k in tfdata["graphdict"]
                            if k.startswith(gvid_table[tail])
                        ]
                    )
                    > 0
                ):
                    conn = gvid_table[tail]
                    conn_type = gvid_table[tail].split(".")[0]
                    # Find out the actual nodes with ~ suffix where link is not specific to a numbered node
                    matched_connections = [
                        k for k in tfdata["graphdict"] if k.startswith(gvid_table[tail])
                    ]
                    matched_nodes = [
                        k for k in tfdata["graphdict"] if k.startswith(gvid_table[head])
                    ]
                    if not node in tfdata["graphdict"] and len(matched_nodes) == 1:
                        node = matched_nodes[0]
                    if (
                        not conn in tfdata["graphdict"]
                        and len(matched_connections) == 1
                    ):
                        conn = matched_connections[0]
                    if conn_type in REVERSE_ARROW_LIST:
                        if not conn in tfdata["graphdict"].keys():
                            tfdata["graphdict"][conn] = list()
                        tfdata["graphdict"][conn].append(node)
                    else:
                        tfdata["graphdict"][node].append(conn)
    tfdata = add_vpc_implied_relations(tfdata)
    tfdata["original_graphdict"] = dict(tfdata["graphdict"])
    tfdata["original_metadata"] = dict(tfdata["meta_data"])
    # TODO: Add a helper function to detect _aws, azurerm and google provider prefixes on resource names
    if len(helpers.list_of_dictkeys_containing(tfdata["graphdict"], "aws_")) == 0:
        click.echo(
            click.style(
                f"\nERROR: No AWS, Azure or Google resources will be created with current plan. Exiting.",
                fg="red",
                bold=True,
            )
        )
        exit()
    return tfdata


# Handle VPC / Subnet relationships
def add_vpc_implied_relations(tfdata: dict):
    vpc_resources = [
        k
        for k, v in tfdata["graphdict"].items()
        if helpers.get_no_module_name(k).startswith("aws_vpc.")
    ]
    subnet_resources = [
        k
        for k, v in tfdata["graphdict"].items()
        if helpers.get_no_module_name(k).startswith("aws_subnet.")
    ]
    if len(vpc_resources) > 0 and len(subnet_resources) > 0:
        for vpc in vpc_resources:
            vpc_cidr = ipaddr.IPNetwork(tfdata["meta_data"][vpc]["cidr_block"])
            for subnet in subnet_resources:
                subnet_cidr = ipaddr.IPNetwork(
                    tfdata["meta_data"][subnet]["cidr_block"]
                )
                if subnet_cidr.overlaps(vpc_cidr):
                    tfdata["graphdict"][vpc].append(subnet)
    return tfdata
