resource "yandex_compute_instance" "svalinn" {
  name               = "svalinn"
  hostname           = "svalinn"
  folder_id          = var.folder_id
  zone               = "ru-central1-e"
  platform_id        = "standard-v3"
  maintenance_policy = "unspecified"

  boot_disk {
    initialize_params {
      name       = "disk-ubuntu-24-04-lts-1795258515235"
      type       = "network-ssd"
      size       = 10
      block_size = 4096
      image_id   = "fd8u5v27dsuh7m2i274r"
    }
    auto_delete = true
  }

  metadata = {
    user-data = templatefile("${path.module}/cloud-init.yaml", {
      ssh_public_key = var.ssh_public_key
      ssh_user       = var.ssh_user
    })
    ssh-keys                = "${var.ssh_user}:${var.ssh_public_key}"
    private_ui_created_from = "console"
  }

  network_interface {
    subnet_id = var.subnet_id
    index     = 0
    nat       = true
  }

  resources {
    memory        = 2
    cores         = 2
    core_fraction = 100
  }

  scheduling_policy {
    preemptible = false
  }
}
