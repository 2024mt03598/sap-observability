terraform {
  required_version = ">= 1.5.0"

  # Remote state so the GitHub pipeline and your laptop share one state.
  # storage_account_name is passed at init time:
  #   terraform init -backend-config="storage_account_name=<name>"
  backend "azurerm" {
    resource_group_name = "datapipeline_group"
    container_name      = "tfstate"
    key                 = "observability.tfstate"
    use_azuread_auth    = true
  }

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  subscription_id = "cedc5e01-c693-4b4e-8ccb-ce00f973a598"

  # Skip auto-registration of resource providers (needs subscription-level rights)
  resource_provider_registrations = "none"

  features {
    virtual_machine {
      delete_os_disk_on_deletion = true # OS disk is removed on terraform destroy
    }
  }
}

# ---------------- Variables ----------------
variable "admin_password" {
  description = "Password for r3user"
  type        = string
  sensitive   = true
}

variable "location" {
  type    = string
  default = "southeastasia"
}

variable "resource_group_name" {
  type    = string
  default = "datapipeline_group"
}

variable "vnet_name" {
  description = "Existing VNet to join"
  type        = string
  default     = "vnet-southeastasia"
}

variable "subnet_name" {
  description = "Existing subnet to join"
  type        = string
  default     = "snet-southeastasia-1"
}

variable "vnet_resource_group" {
  description = "Resource group of the existing VNet"
  type        = string
  default     = "datapipeline_group"
}

variable "allowed_source_cidr" {
  description = "Public IP/CIDR allowed to reach SSH, Prometheus, Alertmanager and Grafana, e.g. 203.0.113.10/32"
  type        = string
}

variable "alloy_targets" {
  description = "OPTIONAL: scrape Alloy's HTTP server (host:12345). Not needed when Alloy pushes via remote_write."
  type        = list(string)
  default     = []
}

variable "alloy_unix_label" {
  description = "Label of the prometheus.exporter.unix component in the Alloy config (prometheus.exporter.unix \"<label>\"). Set to \"\" to skip scraping host metrics from Alloy."
  type        = string
  default     = "default"
}

variable "monitored_targets" {
  description = "Machines running plain node_exporter (host:9100). Empty by default."
  type        = list(string)
  default     = []
}

variable "vm_size" {
  description = "Matches the VM that is deployed today. Change deliberately (resizing reboots the VM)."
  type        = string
  default     = "Standard_B2ls_v2"
}

variable "os_disk_storage_type" {
  description = "Changing this forces the VM to be replaced"
  type        = string
  default     = "Standard_LRS"
}

variable "private_ip" {
  description = "Fixed private IP for this VM inside the subnet (recommended: Alloy pushes to it). Leave empty for dynamic."
  type        = string
  default     = ""
}

variable "vm_name" {
  type    = string
  default = "r3server-tf"
}

# Monitoring stack versions (bump as needed)
variable "prometheus_version" {
  type    = string
  default = "3.5.0"
}

variable "alertmanager_version" {
  type    = string
  default = "0.28.1"
}

variable "node_exporter_version" {
  type    = string
  default = "1.9.1"
}

# ---------------- Existing resources (looked up only, never modified) ----------------
data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

data "azurerm_subnet" "existing" {
  name                 = var.subnet_name
  virtual_network_name = var.vnet_name
  resource_group_name  = var.vnet_resource_group
}

# ---------------- Public IP ----------------
resource "azurerm_public_ip" "pip" {
  name                = "${var.vm_name}-ip"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  allocation_method   = "Static"
  sku                 = "Standard"
  zones               = ["2"]
}

# ---------------- NSG: only your IP can reach these ports ----------------
resource "azurerm_network_security_group" "nsg" {
  name                = "${var.vm_name}-nsg"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name

  security_rule {
    name                       = "Allow-SSH-and-Monitoring"
    priority                   = 1001
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["22", "9090", "9093", "3000"]
    source_address_prefix      = var.allowed_source_cidr
    destination_address_prefix = "*"
  }
}

# ---------------- Network interface ----------------
resource "azurerm_network_interface" "nic" {
  name                = "${var.vm_name}-nic"
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name

  ip_configuration {
    name                          = "ipconfig1"
    subnet_id                     = data.azurerm_subnet.existing.id
    private_ip_address_allocation = var.private_ip == "" ? "Dynamic" : "Static"
    private_ip_address            = var.private_ip == "" ? null : var.private_ip
    public_ip_address_id          = azurerm_public_ip.pip.id
  }
}

resource "azurerm_network_interface_security_group_association" "nic_nsg" {
  network_interface_id      = azurerm_network_interface.nic.id
  network_security_group_id = azurerm_network_security_group.nsg.id
}

# ---------------- Virtual Machine ----------------
resource "azurerm_linux_virtual_machine" "vm" {
  name                = var.vm_name
  computer_name       = var.vm_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  zone                = "2"
  size                = var.vm_size

  admin_username                  = "r3user"
  admin_password                  = var.admin_password
  disable_password_authentication = false

  network_interface_ids = [azurerm_network_interface.nic.id]

  provision_vm_agent         = true
  allow_extension_operations = true
  patch_mode                 = "ImageDefault"
  patch_assessment_mode      = "ImageDefault"

  identity {
    type = "SystemAssigned"
  }

  os_disk {
    name                 = "${var.vm_name}-osdisk"
    caching              = "ReadWrite"
    storage_account_type = var.os_disk_storage_type
  }

  source_image_reference {
    publisher = "suse"
    offer     = "sles-15-sp5-basic"
    sku       = "gen2"
    version   = "latest"
  }

  boot_diagnostics {} # Azure-managed storage

  # Config is now managed by the GitHub pipeline, so edits to the first-boot
  # script must never trigger an automatic VM replacement.
  lifecycle {
    ignore_changes = [custom_data]
  }

  # Runs once on first boot (cloud-init): installs Prometheus, Alertmanager,
  # node_exporter and Grafana. Log: /var/log/monitoring-install.log
  custom_data = base64encode(templatefile("${path.module}/install_monitoring.sh.tftpl", {
    prometheus_version    = var.prometheus_version
    alertmanager_version  = var.alertmanager_version
    node_exporter_version = var.node_exporter_version
    monitored_targets     = var.monitored_targets
    alloy_targets         = var.alloy_targets
    alloy_unix_label      = var.alloy_unix_label
  }))
}

# ---------------- Extension ----------------
resource "azurerm_virtual_machine_extension" "aad_ssh" {
  name                       = "AADSSHLoginForLinux"
  virtual_machine_id         = azurerm_linux_virtual_machine.vm.id
  publisher                  = "Microsoft.Azure.ActiveDirectory"
  type                       = "AADSSHLoginForLinux"
  type_handler_version       = "1.0"
  auto_upgrade_minor_version = true
}

# ---------------- Outputs ----------------
output "public_ip" {
  value = azurerm_public_ip.pip.ip_address
}

output "private_ip" {
  value = azurerm_network_interface.nic.private_ip_address
}

output "alloy_remote_write_url" {
  description = "Put this URL in prometheus.remote_write on the SAP/Oracle VM's Alloy"
  value       = "http://${azurerm_network_interface.nic.private_ip_address}:9090/api/v1/write"
}

output "ssh_command" {
  value = "ssh r3user@${azurerm_public_ip.pip.ip_address}"
}

output "service_urls" {
  value = {
    prometheus   = "http://${azurerm_public_ip.pip.ip_address}:9090"
    alertmanager = "http://${azurerm_public_ip.pip.ip_address}:9093"
    grafana      = "http://${azurerm_public_ip.pip.ip_address}:3000"
  }
}
