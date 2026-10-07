FROM public.ecr.aws/docker/library/python:3.12-slim

# Lambda Web Adapter (includes Runtime Interface Client for non-AWS base images).
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /var/task

ENV PORT=8080
ENV AWS_LWA_INVOKE_MODE=response_stream
ENV AWS_LWA_READINESS_CHECK_PATH=/health

# Install dependencies first for layer caching
COPY api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code. The agent's flat top-level layout (agent.py,
# checkpointer.py, registry.py, step_loader.py, tools/, plans/, config/) is
# preserved as-is rather than restructured, matching the migration
# playbook's guidance to keep agent-specific code changes minimal.
COPY agent.py ./
COPY checkpointer.py ./
COPY registry.py ./
COPY step_loader.py ./
COPY tools/ ./tools/
COPY plans/ ./plans/
COPY config/ ./config/
COPY api/ ./api/

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
