# Agent image = your existing Claude Code image + Python 3 and Node.js.
#
# Sandboxes cannot install anything at run time (no package registry is reachable), so every
# tool an agent uses must be in the image. Claude Code expects python3 and node for scripts.
#
#   docker build --build-arg BASE=<your current agent image> -t <registry>/legion-agent:1 images/agent
#   docker push <registry>/legion-agent:1
#   # orchestrator.yaml: sandbox.image: <registry>/legion-agent:1  (and verifier_image if the
#   # verification commands need them too)
#
# Assumes a Debian/Ubuntu-based BASE (apt). Everything installs under /usr, which the sandbox
# policy mounts read-only, so nothing else needs changing.
ARG BASE
FROM ${BASE}

# the user the base image runs as (sandbox.run_as_user)
ARG AGENT_USER=agent

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        nodejs npm \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    # `python` as well as `python3`: agents and scripts use both
    && ln -sf /usr/bin/python3 /usr/local/bin/python \
    && python3 --version && node --version && npm --version
USER ${AGENT_USER}
