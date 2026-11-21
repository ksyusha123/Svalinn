output "instance_id" {
  description = "ID of the Svalinn VM."
  value       = yandex_compute_instance.svalinn.id
}

output "public_ip" {
  description = "Public IPv4 address of the Svalinn VM."
  value       = yandex_compute_instance.svalinn.network_interface[0].nat_ip_address
}

output "ssh_command" {
  description = "SSH command; use the private key matching the configured public key."
  value       = "ssh ${var.ssh_user}@${yandex_compute_instance.svalinn.network_interface[0].nat_ip_address}"
  sensitive   = true
}
