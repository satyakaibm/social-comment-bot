locals {
  required_apis = [
    "compute.googleapis.com",
    "oslogin.googleapis.com",
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
