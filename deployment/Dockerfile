# syntax=docker/dockerfile:1
ARG NODE_BASE=node:26.13.0-bookworm-slim
ARG PYTHON_BASE=python:3.11.15-slim-bookworm
FROM ${NODE_BASE} AS node
FROM ${PYTHON_BASE} AS base
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libstdc++6 libatomic1 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libx11-6 libxcb1 libxext6 \
    fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && test "$(node --version)" = "v26.13.0" \
    && python -c "import sys; assert sys.version_info[:3] == (3,11,15)"

FROM base AS build
WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock pyproject.toml ./
COPY deployment/requirements-server.lock deployment/requirements-server.lock
RUN python -m pip install --no-cache-dir --prefix=/install -r deployment/requirements-server.lock
COPY src/ src/
# Build using the exact setuptools/wheel already locked above.
RUN PYTHONPATH=/install/lib/python3.11/site-packages python -m pip wheel \
    --no-cache-dir --no-deps --no-build-isolation --wheel-dir=/wheels . \
    && python -m pip install --no-cache-dir --no-deps --prefix=/install /wheels/*.whl
COPY deployment/install_runtime.py deployment/install_vendor_tools.py /build/deployment/
COPY deployment/build-input/ /build/input/
ARG RUNTIME_ARCHIVE_SHA256
ARG RUNTIME_MANIFEST_SHA256
ARG UI_COMMIT
RUN mkdir -p /opt/sfdc \
    && python deployment/install_runtime.py --archive input/sfdc-node-runtime.tar.gz \
       --artifacts input/artifacts.json --destination /opt/sfdc/node-runtime \
       --expected-sha256 "${RUNTIME_ARCHIVE_SHA256}" --expected-commit "${UI_COMMIT}" \
       --expected-manifest-sha256 "${RUNTIME_MANIFEST_SHA256}" --receipt /opt/sfdc/runtime-provenance.json \
    && cd /opt/sfdc/node-runtime \
    && npm ci --omit=dev --ignore-scripts --no-audit --no-fund
RUN python deployment/install_vendor_tools.py --contract input/vendor-tools.json --destination /opt/sfdc/vendor

FROM base AS server
COPY --from=build /install/ /usr/local/
COPY --from=build /opt/sfdc/ /opt/sfdc/
# Playwright's existing channel:'chrome' has a fixed Linux path. It is only
# linked when the approved Chrome contract was installed and version-checked.
RUN python -c "import json,pathlib; p=pathlib.Path('/opt/sfdc/vendor/vendor-tools.json'); c=json.loads(p.read_text())['tools'].get('chrome'); target=pathlib.Path('/opt/google/chrome'); target.mkdir(parents=True,exist_ok=True) if c else None; (target/'chrome').symlink_to(c['path']) if c else None" \
    && groupadd --gid 10001 sfdc \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /var/lib/sfdc-agent/empty-home --shell /usr/sbin/nologin sfdc \
    && mkdir -p /var/lib/sfdc-agent /run/sfdc-agent \
    && chown 10001:10001 /var/lib/sfdc-agent /run/sfdc-agent \
    && chmod 0700 /var/lib/sfdc-agent /run/sfdc-agent \
    && python -m pip check
ENV SFDC_AGENT_SERVER_MODE=1 SFDC_AGENT_MANAGED_ONLY=1 \
    SFDC_MANAGED_ENGINE_ROOT=/opt/sfdc/node-runtime \
    SFDC_MANAGED_EXECUTION_NODE=/usr/local/bin/node \
    SFDC_AGENT_ROOT=/var/lib/sfdc-agent TMPDIR=/run/sfdc-agent \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
WORKDIR /var/lib/sfdc-agent
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD ["python", "-c", "import json,urllib.request; p=json.load(open('/etc/sfdc/agent-server-contract.json'))['network']['port']; urllib.request.urlopen('http://127.0.0.1:'+str(p)+'/health', timeout=2).read()"]
ENTRYPOINT ["python", "-m", "sfdc_agent.server_runtime"]
CMD ["--contract", "/etc/sfdc/agent-server-contract.json"]
