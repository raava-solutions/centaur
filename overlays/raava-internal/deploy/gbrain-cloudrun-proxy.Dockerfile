FROM gcr.io/google.com/cloudsdktool/google-cloud-cli:slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends google-cloud-cli-cloud-run-proxy

ENTRYPOINT ["/usr/bin/cloud-run-proxy"]
