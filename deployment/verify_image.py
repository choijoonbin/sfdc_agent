"""Run the core Linux image with synthetic configuration and zero provider calls."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


def docker(*arguments):
    return subprocess.check_output(["docker", *arguments], text=True).strip()


def verify(image, output):
    root = Path(__file__).resolve().parent
    contract = json.loads((root / "agent-server-contract.example.json").read_text())
    receipt = json.loads(docker("run", "--rm", "--network", "none", "--entrypoint", "python", image,
                                "-c", "from pathlib import Path; print(Path('/opt/sfdc/runtime-provenance.json').read_text())"))
    contract["artifact"] = {key: receipt[key] for key in contract["artifact"]}
    scope = contract["worker"]["scope"]
    token = "synthetic-image-check-token-not-a-real-credential"
    name = "sfdc-agent-readiness-" + uuid4().hex[:12]
    checks = []
    with tempfile.TemporaryDirectory(prefix="sfdc-agent-image-check-") as temporary:
        folder = Path(temporary)
        config = folder / "agent-server-contract.json"
        config.write_text(json.dumps(contract))
        config.chmod(0o644)
        env = folder / "synthetic.env"
        env.write_text("\n".join(f"{key}={value}" for key, value in {
            "SFDC_STORAGE_PROVIDER": "s3", "AWS_REGION": contract["storage"]["region"],
            "SFDC_STORAGE_S3_BUCKET": contract["storage"]["bucket"], "SFDC_STORAGE_S3_PREFIX": contract["storage"]["prefix"],
            "SFDC_AGENT_TRUSTED_HOSTS": ",".join(contract["network"]["trustedHosts"]),
            "SFDC_MANAGED_AGENT_ORIGIN": contract["network"]["selfOrigin"], "SFDC_MANAGED_RUNTIME_SHA256": receipt["runtimeSha256"],
            "SFDC_MANAGED_FIGMA_SCOPE": scope["tenantId"] + "/" + scope["workspaceId"],
            "SFDC_AGENT_EXECUTION_TOKEN": token,
            "SFDC_AGENT_EXECUTION_TOKEN_REF": contract["secretReferences"]["executionToken"],
            "AWS_EC2_METADATA_DISABLED": "true"}.items()) + "\n")
        env.chmod(0o600)
        try:
            docker("run", "--detach", "--name", name, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--tmpfs", "/var/lib/sfdc-agent:rw,uid=10001,gid=10001,mode=0700",
                   "--tmpfs", "/run/sfdc-agent:rw,uid=10001,gid=10001,mode=0700",
                   "--mount", f"type=bind,source={config},target=/etc/sfdc/agent-server-contract.json,readonly",
                   "--env-file", str(env), "--publish", "127.0.0.1::8766", image)
            port = docker("port", name, "8766/tcp").split(":")[-1]
            origin = "http://127.0.0.1:" + port

            def request(path, *, payload=None, authenticated=False):
                headers = {"Content-Type": "application/json"}
                if authenticated:
                    headers["Authorization"] = "Bearer " + token
                body = json.dumps(payload).encode() if payload is not None else None
                req = Request(origin + path, data=body, headers=headers)
                try:
                    with urlopen(req, timeout=3) as response:
                        return response.status, json.loads(response.read())
                except HTTPError as error:
                    return error.code, json.loads(error.read())

            for _ in range(100):
                try:
                    status, health = request("/health")
                    if status == 200:
                        break
                except (URLError, TimeoutError, ConnectionError):
                    pass
                if docker("inspect", "--format", "{{.State.Running}}", name) != "true":
                    raise RuntimeError("Server image exited before health; inspect its startup error code.")
                time.sleep(0.2)
            else:
                raise RuntimeError("Server image did not become locally healthy.")
            assert health["mode"] == "managed"
            checks.append("managed_liveness")
            assert request("/internal/v1/server-readiness")[0] == 401
            status, readiness = request("/internal/v1/server-readiness", authenticated=True)
            assert status == 200 and readiness["startupReady"] is True and readiness["deploymentReady"] is False
            assert readiness["externalServices"] == "NOT_CONTACTED" and readiness["capabilities"]["figmaMcp"] == "NOT_READY"
            checks.append("service_authenticated_truthful_readiness")
            assert request("/runs", payload={})[0] == 404
            assert request("/internal/v1/executions", payload={})[0] == 401
            assert request("/internal/v1/executions", payload={}, authenticated=True)[0] == 422
            checks.append("managed_boundary_existing_service_auth_and_validation")
            bad_scope = {"executionScope": {"tenantId": str(uuid4()), "workspaceId": scope["workspaceId"]}}
            assert request("/internal/v1/source-discovery", payload=bad_scope, authenticated=True)[0] == 403
            checks.append("cross_scope_denied_before_storage")
            for path, payload, code in (("/internal/v1/figma-mcp/collect", {}, "FIGMA_MCP_SERVER_NOT_READY"),
                     ("/internal/v1/executions", {"sourceAcquisition": "MCP_ONLY"}, "FIGMA_MCP_SERVER_NOT_READY"),
                     ("/internal/kiro/sessions", {}, "KIRO_SERVER_DISABLED"),
                     ("/internal/v1/visual-comparisons", {}, "VISUAL_CAPTURE_SERVER_UNAVAILABLE")):
                status, response = request(path, payload=payload, authenticated=True)
                assert status == 503 and response["error"]["code"] == code
            checks.append("mcp_kiro_chrome_unavailable_without_silent_fallback")
            assert docker("exec", name, "id", "-u") == "10001"
            assert docker("inspect", "--format", "{{.HostConfig.ReadonlyRootfs}}", name) == "true"
            checks.append("nonroot_readonly_without_source_mount")
            image_info = json.loads(docker("image", "inspect", image))[0]
            report = {"schemaVersion": 1, "kind": "sfdc-agent-image-verification", "passed": True,
                      "image": image, "imageId": image_info["Id"], "platform": image_info["Os"] + "/" + image_info["Architecture"],
                      "runtime": receipt, "checks": checks, "externalProviders": "NOT_CONTACTED", "deploymentReady": False}
            Path(output).write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report))
            return report
        except BaseException:
            # Startup messages contain constant policy codes and public local
            # readiness only. Do not print container environment or secret files.
            subprocess.run(["docker", "logs", "--tail", "20", name], check=False)
            raise
        finally:
            subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    verify(args.image, args.output)
