output "instance_name" {
  value = google_compute_instance.bot.name
}

output "external_ip" {
  description = "Ephemeral public IP. No inbound firewall rules are open besides IAP-only SSH -- this address doesn't need to be reachable for the bot to work, since Cloudflare Tunnel connects outbound."
  value       = google_compute_instance.bot.network_interface[0].access_config[0].nat_ip
}

output "ssh_command" {
  description = "Connects over IAP tunneling (works even with no inbound rule for your own IP)."
  value       = "gcloud compute ssh ${google_compute_instance.bot.name} --zone=${var.zone} --tunnel-through-iap --project=${var.project_id}"
}
