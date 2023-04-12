ARG SR_LINUX_RELEASE
FROM srl/auto-config-v2:$SR_LINUX_RELEASE
# FROM srl/custombase:$SR_LINUX_RELEASE
# FROM ghcr.io/nokia/srlinux:$SR_LINUX_RELEASE
# FROM registry.srlinux.dev/pub/srlinux:$SR_LINUX_RELEASE

RUN sudo mkdir --mode 0755 -p /etc/opt/srlinux/appmgr/ /opt/demo-agents/
COPY --chown=srlinux:srlinux ./context-agent.yml /etc/opt/srlinux/appmgr
COPY ./src /opt/demo-agents/

# run pylint to catch any obvious errors
RUN PYTHONPATH=$AGENT_PYTHONPATH pylint --load-plugins=pylint_protobuf -E /opt/demo-agents/context-agent

# Using a build arg to set the release tag, set a default for running docker build manually
ARG SRL_CONTEXT_AGENT_RELEASE="[custom build]"
ENV SRL_CONTEXT_AGENT_RELEASE=$SRL_CONTEXT_AGENT_RELEASE
