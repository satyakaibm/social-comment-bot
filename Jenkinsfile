// Build+test and deploy must be started manually from the Jenkins console --
// no SCM/webhook trigger fires this job on a GitHub push or merge, and no
// approval prompt sits between a successful build and deploying to the
// production VM (social-comment-bot on GCP); starting the job manually is
// the approval. Deploy still only ever runs for the master branch. agent any
// on the controller's own Docker Desktop daemon and an env.GIT_BRANCH-based
// branch check rather than `when { branch }` mirror content_my_trip's
// Jenkinsfile conventions; this is a plain Pipeline job, not multibranch.
pipeline {
    agent any

    options {
        disableConcurrentBuilds()
        timestamps()
    }

    environment {
        GIT_SHA = "${env.GIT_COMMIT}"
        // Pinned by digest (see content_my_trip's TRIVY_IMAGE for the same
        // convention) rather than a mutable :stable tag. This is the
        // *only* place gcloud is invoked for this project -- google/cloud-sdk
        // does not ship an ssh client even in this variant, so the deploy
        // stage below installs openssh-client on the fly before `gcloud
        // compute ssh` runs.
        CLOUD_SDK_IMAGE = 'google/cloud-sdk@sha256:921379436032457bf768c75bdedbcdd22b763cdde56a0dbd7043a06d26435634'
    }

    stages {
        stage('Test') {
            steps {
                // Runs pytest as a `RUN` step during the image build itself,
                // rather than `docker run -v $PWD:...` against a throwaway
                // image. Deliberate: this controller's `docker` CLI talks to
                // the *host's* Docker Desktop daemon (docker.sock mount), so
                // `-v host:container` paths resolve against the macOS host,
                // not this container's own filesystem -- a `-v "$PWD:/app"`
                // mount here would silently bind the wrong (or a
                // nonexistent) directory. `docker build` has no such
                // problem: its context is streamed to the daemon over the
                // API, so it works regardless of where the client runs.
                sh 'docker build -f Dockerfile.test -t social-comment-bot-test:$GIT_SHA .'
            }
        }

        stage('Build image') {
            steps {
                // Validates the real production Dockerfile builds cleanly.
                // Nothing is pushed anywhere -- the VM builds its own copy
                // from source in the Deploy stage below (`docker compose up
                // -d --build` after `git pull`), so there's no image
                // registry in this project's deploy path to push to.
                sh 'docker build -t social-comment-bot:$GIT_SHA .'
            }
        }

        stage('Deploy to VM') {
            when {
                expression { env.GIT_BRANCH?.endsWith('/master') || env.GIT_BRANCH == 'master' }
            }
            steps {
                // gcp-wif-oidc-token is the Jenkins "OpenID Connect id
                // token" credential (oidc-provider plugin). Bound here as a
                // plain string -- the plugin's credential implementation
                // doubles as a StringCredentials, so this yields the raw
                // signed JWT with issuer https://jenkins.hindolroad.download/oidc
                // and audience gcp-social-comment-bot-deploy. Jenkins masks
                // its value in the build log automatically like any other
                // credential binding.
                withCredentials([string(credentialsId: 'gcp-wif-oidc-token', variable: 'ID_TOKEN')]) {
                    sh '''
                        set -eu
                        # -e ID_TOKEN (no literal value) forwards the value
                        # from this shell's own environment into the
                        # container -- never written to a file or argv on
                        # the host side, and never appears in this script's
                        # own text, so there's nothing here for Jenkins to
                        # need to mask beyond what the credential binding
                        # already does.
                        docker run --rm -i --platform linux/amd64 -e ID_TOKEN "$CLOUD_SDK_IMAGE" bash -s <<'DEPLOY'
set -eu

apt-get update -qq
apt-get install -y --no-install-recommends openssh-client -qq

echo "$ID_TOKEN" > /tmp/id_token

# Workload Identity Federation config -- see terraform/main.tf
# (google_iam_workload_identity_pool.jenkins /
# google_iam_workload_identity_pool_provider.jenkins_oidc). No static
# service-account key exists anywhere for this deploy identity: GCP verifies
# the JWT above against the pool provider (issuer + audience match, live
# signature check against https://jenkins.hindolroad.download/oidc/jwks)
# before minting a short-lived access token for jenkins-deployer via
# impersonation. If the pool/provider ID or deployer SA is ever renamed,
# these two values must be updated to match Terraform's outputs
# (jenkins_workload_identity_provider / jenkins_deployer_email).
cat > /tmp/cred-config.json <<'JSON'
{
  "type": "external_account",
  "audience": "//iam.googleapis.com/projects/361986615603/locations/global/workloadIdentityPools/jenkins-pool/providers/jenkins-oidc",
  "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
  "token_url": "https://sts.googleapis.com/v1/token",
  "service_account_impersonation_url": "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/jenkins-deployer@project-e1de8eb7-3b06-4142-9b3.iam.gserviceaccount.com:generateAccessToken",
  "credential_source": { "file": "/tmp/id_token" }
}
JSON

gcloud auth login --cred-file=/tmp/cred-config.json --quiet
gcloud config set project project-e1de8eb7-3b06-4142-9b3 --quiet

# --tunnel-through-iap: the VM has no SSH port open to the internet at all
# (see terraform/main.tf's allow_iap_ssh firewall rule, restricted to
# Google's IAP forwarding range) -- this is the only way in. StrictHostKey
# Checking is disabled because this is a fresh, disposable container every
# run with no persisted known_hosts, reaching a specific instance identified
# by project+zone+name over Google's own IAP tunnel rather than a raw
# internet address, so there's no real host-spoofing exposure here to check
# against.
# Deploy through rebuild_all_containers.sh rather than a bare `docker compose
# up -d --build`. That bare form names no tenant compose file, so it rebuilt
# the images but recreated only the main instance's three containers; every
# tenant kept running whatever image it was created with and fell behind
# silently. On 2026-10-04 travel_explorer_satya's video-stats and
# youtube-comments were two days and one merged PR behind the main instance,
# both reporting "healthy" throughout, because they were -- just old. The
# script passes every tenant's -f file in one command (so none is an orphan,
# and --remove-orphans is never needed) and skips the youtube-comments
# service for tenants with no YOUTUBE_REFRESH_TOKEN.
# The script always force-recreates. A --no-force variant was tried here and
# removed after it reproduced the very bug this replaces: the rebuild tagged
# a new video-stats image, but only the one container whose env_file had
# changed was recreated, leaving main and travel_explorer_satya on the
# previous image. Compose's change detection is not a guarantee that a
# container ends up on the image just built, so the deploy pays a few
# seconds of restart across the stack instead.
# A flat `sleep 5 && curl` here used to fail the build (exit 56, connection
# reset) purely from timing: gunicorn/the app were still finishing their
# cold start on a freshly recreated container, well after the deploy step
# itself had already succeeded. Retrying for up to a minute absorbs that
# startup variance while still failing the build if the app genuinely never
# comes up. The loop deliberately does not use a shell variable: this command
# crosses the Jenkins shell, the Cloud SDK container shell, and the VM shell,
# and an earlier `$ok` flag was expanded by the wrong `set -u` shell.
# Retried once, and only for one specific failure. Build #105 (2026-10-07)
# died on the VM with "sudo: a password is required" although nothing had
# changed: IAM still granted jenkins-deployer roles/compute.osAdminLogin and
# the VM's sudoers were intact. The serial console showed why --
# "google_authorized_principals: Failed to validate that OS Login user
# sa_... has adminLogin permission; got HTTP response code: 404". Google's
# metadata server flaked the admin check for that one SSH session, so the
# login succeeded but no /var/google-sudoers.d entry was written, and the
# first sudo on the VM failed before touching anything. A re-run 13 minutes
# later passed. Only that exact sudo message triggers the retry: a failed
# git pull, rebuild, or health check still fails the build on the first
# attempt, since re-running those would just repeat a real error more
# slowly. `set +e` plus PIPESTATUS (this runs in the Cloud SDK container's
# bash, not the VM's shell) keeps the ssh exit code while tee captures the
# output to grep.
attempt=1
while :; do
  set +e
  gcloud compute ssh social-comment-bot \
    --zone=us-central1-a --project=project-e1de8eb7-3b06-4142-9b3 --tunnel-through-iap --quiet \
    --command="cd /opt/social-comment-bot && sudo git pull && sudo ./scripts/rebuild_all_containers.sh && { for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do curl -sf http://localhost:9001/api/health >/dev/null 2>&1 && exit 0; sleep 5; done; exit 1; }" \
    -- -o StrictHostKeyChecking=no 2>&1 | tee /tmp/deploy-ssh.log
  rc=${PIPESTATUS[0]}
  set -e
  [ "$rc" -eq 0 ] && break
  if [ "$attempt" -lt 2 ] && grep -q "sudo: a password is required" /tmp/deploy-ssh.log; then
    echo "OS Login adminLogin check flaked (sudo had no sudoers entry); retrying the SSH step in 20s (attempt $((attempt + 1))/2)" >&2
    attempt=$((attempt + 1))
    sleep 20
    continue
  fi
  exit "$rc"
done
DEPLOY
                    '''
                }
            }
        }
    }

    post {
        always {
            // Same rationale as content_my_trip: keeps the host's Docker
            // Desktop VM from filling up with layers/build cache over time.
            sh 'docker image prune -f --filter "until=24h" || true'
        }
    }
}
