# Agent image = your existing Claude Code image + the tools agents use for their own work
# (scripted edits, copying files): Python 3 and rsync.
#
# Sandboxes cannot install anything at run time (no package registry is reachable), so every
# tool an agent uses must be in the image. The project's own toolchain (bun, node, uv, …) does
# not belong here: it comes from the environment stage's profiles (environments/tools), which put
# it in the agent *and* the verifier images. A tool only agents have makes "passes in my sandbox,
# fails in verification" possible – so verification commands may use only their profile's tools,
# and Node.js in particular is not added here (a bun sandbox has no `node`, on purpose).
#
#   docker build --build-arg BASE=<your current agent image> -t <registry>/legion-agent:1 images/agent
#   docker push <registry>/legion-agent:1
#   # orchestrator.yaml: sandbox.image: <registry>/legion-agent:1
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
        rsync \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    # `python` as well as `python3`: agents and scripts use both
    && ln -sf /usr/bin/python3 /usr/local/bin/python \
    && python3 --version && rsync --version | head -1
USER ${AGENT_USER}
