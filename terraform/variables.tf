variable "project_id" {
  description = "GCP project ID to deploy into."
  type        = string
}

variable "region" {
  description = "Region for the VM. us-central1/us-west1/us-east1 are the Always Free e2-micro regions."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Zone for the VM."
  type        = string
  default     = "us-central1-a"
}

variable "machine_type" {
  description = "e2-micro qualifies for the GCP Always Free tier in the regions above."
  type        = string
  default     = "e2-micro"
}

variable "instance_name" {
  description = "Name of the Compute Engine instance."
  type        = string
  default     = "social-comment-bot"
}

variable "boot_disk_size_gb" {
  description = "Boot disk size in GB. Comfortably covers the app, Docker images, and the SQLite database."
  type        = number
  default     = 20
}

variable "gcp_user_email" {
  description = "Google account granted OS Login admin (sudo) access to the VM via gcloud compute ssh."
  type        = string
  default     = "travelexplorersatya@gmail.com"
}

variable "ssh_source_ranges" {
  description = <<-EOT
    CIDR ranges allowed to reach the VM on port 22. Defaults to Google's
    Identity-Aware Proxy (IAP) forwarding range, so SSH only ever works
    through `gcloud compute ssh` (which tunnels over IAP), never from the
    open internet directly -- no need to know or restrict to your own IP.
  EOT
  type        = list(string)
  default     = ["35.235.240.0/20"]
}

variable "jenkins_oidc_issuer_uri" {
  description = <<-EOT
    Issuer URL of the self-hosted Jenkins instance's built-in OIDC provider
    (oidc-provider plugin), used as the trust anchor for Workload Identity
    Federation. Must be HTTPS -- GCP rejects HTTP issuers outright, which is
    why Jenkins is fronted by a dedicated Cloudflare Tunnel hostname
    (jenkins.hindolroad.download) instead of using its bare localhost URL.
    The discovery doc and JWKS under /oidc/* are intentionally left open
    (Cloudflare Access Bypass policy) since GCP needs to fetch them; the
    actual Jenkins UI stays behind Cloudflare Access login.
  EOT
  type        = string
  default     = "https://jenkins.hindolroad.download/oidc"
}

variable "jenkins_oidc_audience" {
  description = "Audience value configured on the Jenkins 'OpenID Connect id token' credential (gcp-wif-oidc-token) that Jenkins pipelines request tokens for."
  type        = string
  default     = "gcp-social-comment-bot-deploy"
}

