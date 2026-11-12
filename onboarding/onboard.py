#!/usr/bin/env python3
"""App-team onboarding engine (pipelines `app_team_onboarding` and `app_team_onboarding_register`).

    python3 onboard.py plan       # read-only: validate inputs, render YAML, report what would be created
    python3 onboard.py propose    # commit the rendered YAML to branch onboarding/<org>/<project>/<app> and open a PR
    python3 onboard.py register   # after the PR merged: import the files from main into Harness, create the trigger

Nothing exists in Harness until the PR is merged, so a rejected PR leaves nothing to clean up,
and what Harness registers is exactly what reviewers approved. Service, pipeline and input
sets are imported as remote (Git Experience) entities: Harness reads them from the merged
files, and later changes are made by PR to those files. Triggers have no Git storage in
Harness, so the trigger is created through the API from its file in the PR.

Existing Harness entities are never updated or deleted; re-running only fills gaps.
Standard library only. Configuration comes from environment variables (see Config); in
register mode the request manifest committed by `propose` supplies the request settings.
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ID_RE = re.compile(r"^[a-zA-Z_][0-9a-zA-Z_]{0,127}$")
TIMEOUT = 30
MANIFEST_KEYS = (
    "ORG_ID", "PROJECT_ID", "APP_NAME", "OWNER_EMAIL", "TEMPLATE_REF", "TEMPLATE_VERSION",
    "DEPLOYMENT_TYPE", "APP_REPO", "APP_GIT_CONNECTOR", "MANIFEST_PATH", "REGISTRY_CONNECTOR",
    "IMAGE_PATH", "ENVIRONMENTS", "INPUT_OVERRIDES", "CREATE_TRIGGER", "TRIGGER_BRANCH",
    "GITX_CONNECTOR", "GITX_REPO", "GITX_BRANCH",
)


def env(name, default=None, required=True):
    val = os.environ.get(name, default)
    if val is None or (required and str(val).strip() == ""):
        die(f"missing required setting {name}")
    return (val or "").strip()


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def to_identifier(raw):
    ident = re.sub(r"[^0-9a-zA-Z_]", "_", raw.strip()).strip("_").lower()
    if ident and ident[0].isdigit():
        ident = f"_{ident}"
    if not ID_RE.match(ident):
        die(f"cannot derive a valid Harness identifier from {raw!r}")
    return ident


def split_scoped(ref):
    """'org.foo' -> ('org', 'foo'); 'account.foo' -> ('account', 'foo'); 'foo' -> ('project', 'foo')."""
    if ref.startswith("org."):
        return "org", ref[4:]
    if ref.startswith("account."):
        return "account", ref[8:]
    return "project", ref


def indent(block, spaces):
    pad = " " * spaces
    return "\n".join(pad + ln if ln.strip() else ln for ln in block.splitlines())


class Config:
    def __init__(self):
        self.base = env("HARNESS_BASE_URL", "https://app.harness.io/gateway").rstrip("/")
        self.account = env("HARNESS_ACCOUNT_ID")
        self.api_key = env("HARNESS_API_KEY")
        self.org = env("ORG_ID", "default")
        self.project = env("PROJECT_ID")
        self.app_name = env("APP_NAME")
        self.app_id = to_identifier(self.app_name)
        self.owner_email = env("OWNER_EMAIL", "", required=False)

        # Blueprint = the org pipeline template every onboarded pipeline references.
        self.template_ref = env("TEMPLATE_REF")
        self.template_version = env("TEMPLATE_VERSION")
        self.blueprint = to_identifier(split_scoped(self.template_ref)[1])

        self.deployment_type = env("DEPLOYMENT_TYPE", "Kubernetes")
        self.app_repo = env("APP_REPO")
        self.app_git_connector = env("APP_GIT_CONNECTOR", "account.github")
        self.manifest_path = env("MANIFEST_PATH", "", required=False)
        self.registry_connector = env("REGISTRY_CONNECTOR", "account.docker_registry")
        self.image_path = env("IMAGE_PATH", "", required=False)

        self.environments = [e.strip() for e in env("ENVIRONMENTS").split(",") if e.strip()]
        self.overrides = self._parse_overrides(env("INPUT_OVERRIDES", "", required=False))

        self.create_trigger = env("CREATE_TRIGGER", "true").lower() == "true"
        self.trigger_branch = env("TRIGGER_BRANCH", "main")

        # Where the entity YAML lives (Harness Git Experience) and the branch PRs target.
        self.gitx_connector = env("GITX_CONNECTOR", "account.github")
        self.gitx_repo = env("GITX_REPO", "HarnessCICD")
        self.gitx_branch = env("GITX_BRANCH", "main")
        self.gitx_root = f".harness/orgs/{self.org}/projects/{self.project}"

        # GitHub Enterprise, used by `propose` to push the branch and open the PR.
        self.github_api = env("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.github_owner = env("GITHUB_OWNER", "prameetu20codes")
        self.github_token = env("GITHUB_TOKEN", "", required=False)

        self.report_path = env("REPORT_PATH", "/harness/onboarding-report.json", required=False)

        self.service_id = self.app_id
        self.pipeline_id = f"{self.app_id}_{self.blueprint}"[:128]
        self.trigger_id = f"{self.app_id}_push_{to_identifier(self.trigger_branch)}"[:128]
        self.request_key = f"{self.org}/{self.project}/{self.app_id}"
        self.pr_branch = f"onboarding/{self.request_key}"
        self.manifest_file = f"onboarding/requests/{self.request_key}.json"

        for label, ident in (("PROJECT_ID", self.project), ("ORG_ID", self.org)):
            if not ID_RE.match(ident):
                die(f"{label}={ident!r} is not a valid Harness identifier")
        if self.deployment_type not in ("Kubernetes", "Ssh"):
            die("DEPLOYMENT_TYPE must be Kubernetes or Ssh")
        if self.deployment_type == "Kubernetes" and not self.manifest_path:
            die("MANIFEST_PATH is required for Kubernetes services")
        if not self.image_path:
            die("IMAGE_PATH is required")
        if not self.environments:
            die("ENVIRONMENTS must list at least one environment")

    def request_settings(self):
        """The resolved request settings, so `register` replays exactly what `propose` used."""
        return {
            "ORG_ID": self.org, "PROJECT_ID": self.project, "APP_NAME": self.app_name,
            "OWNER_EMAIL": self.owner_email, "TEMPLATE_REF": self.template_ref,
            "TEMPLATE_VERSION": self.template_version, "DEPLOYMENT_TYPE": self.deployment_type,
            "APP_REPO": self.app_repo, "APP_GIT_CONNECTOR": self.app_git_connector,
            "MANIFEST_PATH": self.manifest_path, "REGISTRY_CONNECTOR": self.registry_connector,
            "IMAGE_PATH": self.image_path, "ENVIRONMENTS": ",".join(self.environments),
            "INPUT_OVERRIDES": ",".join(f"{k}={v}" for k, v in self.overrides.items()),
            "CREATE_TRIGGER": str(self.create_trigger).lower(), "TRIGGER_BRANCH": self.trigger_branch,
            "GITX_CONNECTOR": self.gitx_connector, "GITX_REPO": self.gitx_repo,
            "GITX_BRANCH": self.gitx_branch,
        }

    @staticmethod
    def _parse_overrides(raw):
        out = {}
        for pair in filter(None, (p.strip() for p in raw.split(","))):
            if "=" not in pair:
                die(f"INPUT_OVERRIDES entry {pair!r} is not key=value")
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
        return out

    def env_ref(self, env_id):
        return env_id if "." in env_id else f"org.{env_id}"

    def input_set_id(self, env_id):
        return to_identifier(f"{self.app_id}_{split_scoped(env_id)[1]}")[:128]


def http(method, url, headers, body=None):
    data = None
    if body is not None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode() or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"message": raw[:500]}
    except urllib.error.URLError as e:
        die(f"{method} {url.split('?')[0]}: {e.reason}")


class Harness:
    def __init__(self, cfg):
        self.cfg = cfg

    def call(self, method, path, scope="project", body=None, content_type="application/json", **params):
        q = {"accountIdentifier": self.cfg.account}
        if scope in ("org", "project"):
            q["orgIdentifier"] = self.cfg.org
        if scope == "project":
            q["projectIdentifier"] = self.cfg.project
        q.update({k: v for k, v in params.items() if v is not None})
        headers = {"x-api-key": self.cfg.api_key, "Harness-Account": self.cfg.account,
                   "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
        return http(method, f"{self.cfg.base}{path}?{urllib.parse.urlencode(q)}", headers, body)

    def exists(self, path, scope="project", **params):
        status, body = self.call("GET", path, scope, **params)
        if status == 200:
            return True
        if status in (400, 404) and _is_not_found(body):
            return False
        die(f"GET {path} returned HTTP {status}: {body.get('message')}")

    def import_params(self, file_path):
        return dict(connectorRef=self.cfg.gitx_connector, repoName=self.cfg.gitx_repo,
                    branch=self.cfg.gitx_branch, filePath=file_path, isForceImport="false")


class GitHub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.repo = f"{cfg.github_api}/repos/{cfg.github_owner}/{cfg.gitx_repo}"
        self.headers = {"Authorization": f"token {cfg.github_token}",
                        "Accept": "application/vnd.github+json"}

    def call(self, method, path, body=None):
        return http(method, f"{self.repo}{path}", self.headers, body)

    def ensure_branch(self, branch, base):
        status, _ = self.call("GET", f"/git/ref/heads/{urllib.parse.quote(branch)}")
        if status == 200:
            return
        status, ref = self.call("GET", f"/git/ref/heads/{urllib.parse.quote(base)}")
        if status != 200:
            die(f"cannot read base branch {base} of {self.cfg.gitx_repo}: HTTP {status} {ref.get('message')}")
        status, body = self.call("POST", "/git/refs", {"ref": f"refs/heads/{branch}",
                                                       "sha": ref["object"]["sha"]})
        if status not in (200, 201):
            die(f"cannot create branch {branch}: HTTP {status} {body.get('message')}")

    def put_file(self, branch, path, content, message):
        url_path = f"/contents/{urllib.parse.quote(path)}"
        status, cur = self.call("GET", f"{url_path}?ref={urllib.parse.quote(branch)}")
        payload = {"message": message, "branch": branch,
                   "content": base64.b64encode(content.encode()).decode()}
        if status == 200:
            if base64.b64decode(cur.get("content", "")).decode(errors="replace") == content:
                return "unchanged"
            payload["sha"] = cur["sha"]
        status, body = self.call("PUT", url_path, payload)
        if status not in (200, 201):
            die(f"cannot commit {path}: HTTP {status} {body.get('message')}")
        return "committed"

    def ensure_pr(self, branch, base, title, body_md):
        head = f"{self.cfg.github_owner}:{branch}"
        status, prs = self.call("GET", f"/pulls?state=open&head={urllib.parse.quote(head)}&base={base}")
        if status == 200 and isinstance(prs, list) and prs:
            return prs[0]["html_url"], "already open"
        status, pr = self.call("POST", "/pulls", {"title": title, "head": branch, "base": base,
                                                   "body": body_md})
        if status not in (200, 201):
            die(f"cannot open PR from {branch}: HTTP {status} {pr.get('message')}")
        return pr["html_url"], "opened"


def _is_not_found(body):
    text = json.dumps(body).lower()
    return any(k in text for k in ("not found", "not_found", "does not exist", "doesn't exist", "not exist"))


# ---------------------------------------------------------------- rendering

def render_service(cfg):
    if cfg.deployment_type == "Kubernetes":
        manifests = f"""      manifests:
        - manifest:
            identifier: {cfg.app_id}_manifests
            type: K8sManifest
            spec:
              store:
                type: Github
                spec:
                  connectorRef: {cfg.app_git_connector}
                  gitFetchType: Branch
                  repoName: {cfg.app_repo}
                  branch: {cfg.trigger_branch}
                  paths:
                    - {cfg.manifest_path}
              skipResourceVersioning: false
"""
        artifact = f"""              type: DockerRegistry
              spec:
                connectorRef: {cfg.registry_connector}
                imagePath: {cfg.image_path}
                tag: <+input>"""
    else:
        manifests = ""
        artifact = f"""              type: ArtifactoryRegistry
              spec:
                connectorRef: {cfg.registry_connector}
                repository: {cfg.image_path}
                repositoryFormat: generic
                artifactPath: <+input>"""
    return f"""service:
  name: {cfg.app_name}
  identifier: {cfg.service_id}
  orgIdentifier: {cfg.org}
  projectIdentifier: {cfg.project}
  description: Onboarded by app_team_onboarding
  tags:
    onboarded_by: app_team_onboarding
  serviceDefinition:
    type: {cfg.deployment_type}
    spec:
{manifests}      artifacts:
        primary:
          primaryArtifactRef: primary
          sources:
            - identifier: primary
{artifact}
"""


def template_inputs_body(raw):
    body = (raw or "").rstrip()
    if body.startswith("templateInputs:"):
        lines = body.splitlines()[1:]
        body = "\n".join(ln[2:] if ln.startswith("  ") else ln for ln in lines)
    return "" if body.strip() in ("", "{}", "null") else body


def render_pipeline(cfg, inputs_body):
    tpl = f"""  template:
    templateRef: {cfg.template_ref}
    versionLabel: "{cfg.template_version}"
"""
    if inputs_body:
        tpl += "    templateInputs:\n" + indent(inputs_body, 6) + "\n"
    return f"""pipeline:
  name: {cfg.app_name} {cfg.blueprint}
  identifier: {cfg.pipeline_id}
  orgIdentifier: {cfg.org}
  projectIdentifier: {cfg.project}
  tags:
    onboarded_by: app_team_onboarding
    blueprint: {cfg.blueprint}
{tpl}"""


def fill_runtime_inputs(yaml_text, cfg, env_id):
    """Replace `key: <+input>` leaves with known values. Text-based on purpose: no PyYAML in the image.

    An override matches either a field key (serviceRef, namespace, ...) or, for
    `- name: X ... value: <+input>` variable entries, the variable name X.
    """
    values = {
        "serviceRef": cfg.service_id,
        "environmentRef": cfg.env_ref(env_id),
        **cfg.overrides,
    }
    out = []
    current_var = None
    for line in yaml_text.splitlines():
        name = re.match(r"^\s*-\s*name:\s*\"?([^\"]+?)\"?\s*$", line)
        if name:
            current_var = name.group(1)
        m = re.match(r"^(\s*-?\s*)([A-Za-z_][\w]*):\s*\"?<\+input>\"?\s*$", line)
        if m:
            key = m.group(2)
            if key == "value" and current_var in cfg.overrides:
                line = f"{m.group(1)}value: {cfg.overrides[current_var]}"
            elif key != "value" and key in values:
                line = f"{m.group(1)}{key}: {values[key]}"
        out.append(line)
    filled = "\n".join(out)
    return filled, len(re.findall(r"<\+input>", filled))


def render_input_set(cfg, env_id, inputs_body):
    """The input set of a thin templateRef pipeline is its template's runtime inputs."""
    filled, unresolved = fill_runtime_inputs(inputs_body, cfg, env_id)
    isid = cfg.input_set_id(env_id)
    yaml_text = f"""inputSet:
  name: {cfg.app_name} {split_scoped(env_id)[1]}
  identifier: {isid}
  orgIdentifier: {cfg.org}
  projectIdentifier: {cfg.project}
  tags:
    onboarded_by: app_team_onboarding
  pipeline:
    identifier: {cfg.pipeline_id}
    template:
      templateInputs:
{indent(filled, 8)}
"""
    return isid, yaml_text, unresolved


def render_trigger(cfg, input_set_id):
    return f"""trigger:
  name: {cfg.app_name} push {cfg.trigger_branch}
  identifier: {cfg.trigger_id}
  enabled: true
  orgIdentifier: {cfg.org}
  projectIdentifier: {cfg.project}
  pipelineIdentifier: {cfg.pipeline_id}
  pipelineBranchName: {cfg.gitx_branch}
  inputSetRefs:
    - {input_set_id}
  tags:
    onboarded_by: app_team_onboarding
  source:
    type: Webhook
    spec:
      type: Github
      spec:
        type: Push
        spec:
          connectorRef: {cfg.app_git_connector}
          autoAbortPreviousExecutions: true
          repoName: {cfg.app_repo}
          payloadConditions:
            - key: targetBranch
              operator: Equals
              value: {cfg.trigger_branch}
          headerConditions: []
          actions: []
"""


def render_all(cfg, inputs_body):
    """Every entity of the request, in creation order: dicts of kind / id / path / yaml / unresolved."""
    root = cfg.gitx_root
    pipe_dir = f"{root}/pipelines/{cfg.pipeline_id}"
    out = [
        {"kind": "service", "id": cfg.service_id, "path": f"{root}/services/{cfg.service_id}.yaml",
         "yaml": render_service(cfg)},
        {"kind": "pipeline", "id": cfg.pipeline_id, "path": f"{root}/pipelines/{cfg.pipeline_id}.yaml",
         "yaml": render_pipeline(cfg, inputs_body)},
    ]
    if inputs_body:
        for env_id in cfg.environments:
            isid, text, unresolved = render_input_set(cfg, env_id, inputs_body)
            out.append({"kind": "inputset", "id": isid, "path": f"{pipe_dir}/input_sets/{isid}.yaml",
                        "yaml": text, "unresolved": unresolved})
    input_sets = [e for e in out if e["kind"] == "inputset"]
    if cfg.create_trigger and input_sets:
        out.append({"kind": "trigger", "id": cfg.trigger_id,
                    "path": f"{pipe_dir}/triggers/{cfg.trigger_id}.yaml",
                    "yaml": render_trigger(cfg, input_sets[0]["id"])})
    return out


def entity_exists(hx, cfg, ent):
    kind, ident = ent["kind"], ent["id"]
    if kind == "service":
        return hx.exists(f"/ng/api/servicesV2/{ident}")
    if kind == "pipeline":
        return hx.exists(f"/pipeline/api/pipelines/summary/{ident}")
    if not hx.exists(f"/pipeline/api/pipelines/summary/{cfg.pipeline_id}"):
        return False
    if kind == "inputset":
        return hx.exists(f"/pipeline/api/inputSets/{ident}", pipelineIdentifier=cfg.pipeline_id)
    return hx.exists(f"/pipeline/api/triggers/{ident}", targetIdentifier=cfg.pipeline_id)


# ---------------------------------------------------------------- modes

class Report:
    def __init__(self, cfg, mode):
        self.data = {"schema": "app-onboarding/2", "mode": mode, "request": cfg.request_key,
                     "entities": [], "errors": [], "warnings": []}

    def add(self, kind, ident, status, path=None, **extra):
        self.data["entities"].append({"kind": kind, "identifier": ident, "status": status,
                                      "git_path": path, **extra})
        print(f"  {kind:<10} {ident:<48} {status}" + (f"  ({path})" if path else ""))

    def error(self, msg):
        self.data["errors"].append(msg)
        print(f"  ERROR: {msg}", file=sys.stderr)

    def warn(self, msg):
        self.data["warnings"].append(msg)
        print(f"  WARN: {msg}")

    def finish(self, path):
        try:
            with open(path, "w") as fh:
                json.dump(self.data, fh, indent=2)
        except OSError:
            pass
        print("ONBOARDING_REPORT " + json.dumps(self.data, separators=(",", ":")))
        sys.exit(1 if self.data["errors"] else 0)


def preflight(cfg, hx, rep):
    print(f"== Preflight {cfg.org}/{cfg.project}")
    if not hx.exists(f"/ng/api/projects/{cfg.project}", scope="org"):
        rep.error(f"project {cfg.project} does not exist in org {cfg.org}; request it through "
                  "before onboarding")
        return None
    tscope, tid = split_scoped(cfg.template_ref)
    status, body = hx.call("GET", f"/template/api/templates/templateInputs/{tid}", tscope,
                           versionLabel=cfg.template_version)
    if status != 200:
        rep.error(f"template {cfg.template_ref}@{cfg.template_version} not readable: {body.get('message')}")
        return None
    for ref in sorted({cfg.gitx_connector, cfg.app_git_connector, cfg.registry_connector}):
        scope, cid = split_scoped(ref)
        if not hx.exists(f"/ng/api/connectors/{cid}", scope):
            rep.error(f"connector {ref} not found")
    for env_id in cfg.environments:
        scope, eid = split_scoped(cfg.env_ref(env_id))
        if not hx.exists(f"/ng/api/environmentsV2/{eid}", scope):
            rep.error(f"environment {cfg.env_ref(env_id)} not found")
    return template_inputs_body(body.get("data"))


def plan_or_propose(mode):
    cfg = Config()
    hx = Harness(cfg)
    rep = Report(cfg, mode)
    inputs_body = preflight(cfg, hx, rep)
    if rep.data["errors"]:
        rep.finish(cfg.report_path)

    print(f"== {mode.upper()}  {cfg.gitx_repo}: {cfg.pr_branch} -> {cfg.gitx_branch}")
    todo = []
    for ent in render_all(cfg, inputs_body):
        if ent.get("unresolved"):
            rep.warn(f"input set {ent['id']}: {ent['unresolved']} runtime input(s) still <+input>; "
                     "set them with INPUT_OVERRIDES, or the trigger cannot run unattended")
        if entity_exists(hx, cfg, ent):
            rep.add(ent["kind"], ent["id"], "exists in Harness, left unchanged", ent["path"])
            continue
        todo.append(ent)
        if mode == "plan":
            rep.add(ent["kind"], ent["id"], "would propose", ent["path"])
            print(indent(ent["yaml"], 6))

    if mode == "plan" or not todo:
        if not todo:
            print("  nothing to propose: every entity already exists")
        rep.finish(cfg.report_path)

    if not cfg.github_token:
        die("GITHUB_TOKEN is required to push the onboarding branch and open the PR")
    gh = GitHub(cfg)
    gh.ensure_branch(cfg.pr_branch, cfg.gitx_branch)
    commit = f"onboarding: {cfg.app_name} ({cfg.project})"
    for ent in todo:
        rep.add(ent["kind"], ent["id"], gh.put_file(cfg.pr_branch, ent["path"], ent["yaml"], commit), ent["path"])
    manifest = cfg.request_settings()
    manifest["files"] = [{"kind": e["kind"], "id": e["id"], "path": e["path"]} for e in todo]
    gh.put_file(cfg.pr_branch, cfg.manifest_file, json.dumps(manifest, indent=2) + "\n", commit)

    files_md = "\n".join(f"- `{e['kind']}` **{e['id']}**: `{e['path']}`" for e in todo)
    warn_md = "\n".join(f"- {w}" for w in rep.data["warnings"]) or "- none"
    body_md = (f"Onboarding request for **{cfg.app_name}** in `{cfg.org}/{cfg.project}`"
               f"{' from ' + cfg.owner_email if cfg.owner_email else ''}.\n\n"
               f"Blueprint: `{cfg.template_ref}@{cfg.template_version}`\n\n"
               f"Files:\n{files_md}\n\nWarnings:\n{warn_md}\n\n"
               "Merging this PR registers these entities in Harness "
               "(`app_team_onboarding_register`). Closing it without merging creates nothing.")
    url, state = gh.ensure_pr(cfg.pr_branch, cfg.gitx_branch,
                              f"Onboard {cfg.app_name} into {cfg.org}/{cfg.project}", body_md)
    print(f"  PR {state}: {url}")
    rep.data["pr_url"] = url
    rep.finish(cfg.report_path)


def register():
    request = env("REQUEST").strip("/")
    repo_dir = env("REPO_DIR")
    if request.startswith("onboarding/"):
        request = request[len("onboarding/"):]
    manifest_path = os.path.join(repo_dir, "onboarding", "requests", f"{request}.json")
    try:
        with open(manifest_path) as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as e:
        die(f"cannot read request manifest {manifest_path}: {e}")
    for k in MANIFEST_KEYS:
        os.environ[k] = manifest.get(k, "")

    cfg = Config()
    hx = Harness(cfg)
    rep = Report(cfg, "register")
    print(f"== REGISTER {cfg.request_key} from {cfg.gitx_repo}@{cfg.gitx_branch}")

    for ent in manifest["files"]:
        local = os.path.join(repo_dir, ent["path"])
        if not os.path.exists(local):
            rep.error(f"{ent['path']} is not on {cfg.gitx_branch}; was the PR merged?")
            continue
        if entity_exists(hx, cfg, ent):
            rep.add(ent["kind"], ent["id"], "exists in Harness, left unchanged", ent["path"])
            continue
        params = hx.import_params(ent["path"])
        if ent["kind"] == "service":
            status, body = hx.call("POST", "/ng/api/servicesV2/import", serviceIdentifier=ent["id"],
                                   body={"serviceName": cfg.app_name, "serviceDescription": ""}, **params)
        elif ent["kind"] == "pipeline":
            status, body = hx.call("POST", "/pipeline/api/pipelines/import", pipelineIdentifier=ent["id"],
                                   body={"pipelineName": f"{cfg.app_name} {cfg.blueprint}",
                                         "pipelineDescription": ""}, **params)
        elif ent["kind"] == "inputset":
            status, body = hx.call("POST", "/pipeline/api/inputSets/import",
                                   pipelineIdentifier=cfg.pipeline_id, inputSetIdentifier=ent["id"],
                                   body={"inputSetName": ent["id"], "inputSetDescription": ""}, **params)
        else:
            with open(local) as fh:
                trigger_yaml = fh.read()
            status, body = hx.call("POST", "/pipeline/api/triggers", body=trigger_yaml,
                                   content_type="application/yaml", targetIdentifier=cfg.pipeline_id)
        if 200 <= status < 300:
            rep.add(ent["kind"], ent["id"], "imported" if ent["kind"] != "trigger" else "created", ent["path"])
        else:
            rep.add(ent["kind"], ent["id"], f"FAILED HTTP {status}", ent["path"])
            rep.error(f"{ent['kind']} {ent['id']}: {body.get('code')} {body.get('message')}")
            if ent["kind"] in ("service", "pipeline"):
                break
    rep.finish(cfg.report_path)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode in ("plan", "propose"):
        plan_or_propose(mode)
    elif mode == "register":
        register()
    else:
        die("usage: onboard.py plan|propose|register")
