variable "folder_id" {
  description = "Yandex Cloud folder ID. Set in secrets.auto.tfvars or TF_VAR_folder_id."
  type        = string
  sensitive   = true
}

variable "subnet_id" {
  description = "Existing subnet ID. Set in secrets.auto.tfvars or TF_VAR_subnet_id."
  type        = string
  sensitive   = true
}

variable "ssh_public_key" {
  description = "Public SSH key. Set in secrets.auto.tfvars or TF_VAR_ssh_public_key."
  type        = string
  sensitive   = true
}

variable "ssh_user" {
  description = "SSH username. Set in secrets.auto.tfvars or TF_VAR_ssh_user."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[a-z_][a-z0-9_-]{0,31}$", var.ssh_user))
    error_message = "SSH username must be a Linux username of up to 32 characters."
  }
}
