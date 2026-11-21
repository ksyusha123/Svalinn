terraform {
  required_version = ">= 1.5.0"

  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = ">= 0.177.0, < 1.0.0"
    }
  }
}

provider "yandex" {
  folder_id = var.folder_id
  zone      = "ru-central1-e"
}
