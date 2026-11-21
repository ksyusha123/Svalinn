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
  folder_id = "b1g1m90mm920k570odpp"
  zone      = "ru-central1-e"
}
