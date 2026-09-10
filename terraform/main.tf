locals {
  required_apis = [
    "compute.googleapis.com",
    "oslogin.googleapis.com",
    "iam.googleapis.com",
    "iap.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.required_apis)
  project  = var.project_id
  service  = each.value

  disable_dependent_services = false
  disable_on_destroy         = false
}

resource "google_compute_network" "vpc" {
  name                    = "${var.instance_name}-vpc"
  auto_create_subnetworks = true

  depends_on = [google_project_service.apis]
}

# The webhook receiver runs behind a Cloudflare Tunnel, which is an
# *outbound* connection the VM initiates -- nothing needs to reach the VM
# from the internet on 80/443. The only inbound access this opens is SSH,
# and only from Google's IAP forwarding range (never the open internet),
# so `gcloud compute ssh` works but nothing else can reach the VM directly.
resource "google_compute_firewall" "allow_iap_ssh" {
  name    = "${var.instance_name}-allow-iap-ssh"
  network = google_compute_network.vpc.name

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = var.ssh_source_ranges
  target_tags   = [var.instance_name]
}

resource "google_compute_instance" "bot" {
  name         = var.instance_name
  machine_type = var.machine_type
  zone         = var.zone
  tags         = [var.instance_name]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = var.boot_disk_size_gb
      type  = "pd-standard"
    }
  }

  network_interface {
    network = google_compute_network.vpc.name

    # Ephemeral public IP. Safe with no inbound firewall rules open besides
    # IAP-only SSH above -- everything else this VM does is outbound
    # (Cloudflare Tunnel, Meta/YouTube/Gemini API calls).
    access_config {}
  }

  metadata = {
    enable-oslogin = "TRUE"
    startup-script = file("${path.module}/startup-script.sh")
  }

  # Comfortably covers the running containers without needing swap.
  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
  }

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "os_login_admin" {
  project = var.project_id
  role    = "roles/compute.osAdminLogin"
  member  = "user:${var.gcp_user_email}"
}

# Dedicated identity for Jenkins to deploy with -- scoped to exactly the two
# things a deploy needs (SSH in as a sudo-capable OS Login user, and use IAP
# to reach the VM's SSH port) rather than reusing a personal account. The key
# for this is deliberately NOT created here (see terraform/README.md) --
# a google_service_account_key resource would put the private key material
# in plain text in this state file, and this project's state isn't stored
# in a remote backend with its own access controls.
resource "google_service_account" "jenkins_deployer" {
  project      = var.project_id
  account_id   = "jenkins-deployer"
  display_name = "Jenkins CI deploy account for ${var.instance_name}"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "jenkins_os_admin_login" {
  project = var.project_id
  role    = "roles/compute.osAdminLogin"
  member  = "serviceAccount:${google_service_account.jenkins_deployer.email}"
}

resource "google_project_iam_member" "jenkins_iap_tunnel" {
  project = var.project_id
  role    = "roles/iap.tunnelResourceAccessor"
  member  = "serviceAccount:${google_service_account.jenkins_deployer.email}"
}

# Lets Jenkins trade its own self-issued OIDC tokens for short-lived GCP
# credentials without ever holding a static service-account key. GCP
# verifies each token against this pool's provider (issuer + audience match,
# signature verified against the issuer's live JWKS) before allowing
# impersonation of jenkins_deployer below.
resource "google_iam_workload_identity_pool" "jenkins" {
  project                   = var.project_id
  workload_identity_pool_id = "jenkins-pool"
  display_name              = "Jenkins CI"
  description               = "Workload Identity Pool for Jenkins-issued OIDC tokens deploying ${var.instance_name}"

  depends_on = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "jenkins_oidc" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.jenkins.workload_identity_pool_id
  workload_identity_pool_provider_id = "jenkins-oidc"
  display_name                       = "Jenkins OIDC"

  attribute_mapping = {
    "google.subject" = "assertion.sub"
  }

  oidc {
    issuer_uri        = var.jenkins_oidc_issuer_uri
    allowed_audiences = [var.jenkins_oidc_audience]
  }
}

# Scoped to every identity asserted through this pool rather than a specific
# subject claim -- issuer + audience are already locked to this one Jenkins
# credential above, which is sufficient isolation for a single-tenant
# instance. Deliberately not scoped by subject since the oidc-provider
# plugin's `sub` claim isn't a stable, documented value to pin against.
resource "google_service_account_iam_member" "jenkins_wif_impersonation" {
  service_account_id = google_service_account.jenkins_deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.jenkins.name}/*"
}
