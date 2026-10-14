import os
import time
import datetime
import oci

# ╔══════════════════════════════════════════════════════╗
# ║  Target : VM.Standard.A1.Flex (ARM)                  ║
# ║  Spec   : 4 OCPU / 24 GB RAM / 150 GB disk           ║
# ║  Arch   : ARM (Ampere A1)                            ║
# ║  Tier   : Oracle Always Free                         ║
# ║  OS     : Canonical Ubuntu 24.04 LTS                 ║
# ╚══════════════════════════════════════════════════════╝

# ─── Configuration ───────────────────────────────────────
COMPARTMENT_ID = os.getenv(
    "COMPARTMENT_ID",
    "ocid1.tenancy.oc1..aaaaaaaaax5az7tlbtspgjz7pdrwhd2h2dvas6bxdzshjhphsqm2ex5mhxuq"
)
SSH_PUBLIC_KEY = os.getenv(
    "SSH_PUBLIC_KEY",
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDa7iBmuwYkKvaKbgrUucjn/AxN3wDV4IcnS/xs9PhzPGjvBHXrJZYRr4B7i2YNUxHNgzOseOS5Ca7wN1nrXsPZDAnvU308mkV3TDxiIeyobIKUXj7Xb8jvVZmULmHzWb1Zc0KpcewuV4IDfGkT0FjnO1v5ZoiJQ16K7wxF2Y5qugiI4STJzdnoGNfgXmx8WC/lV0vLQ6DhImWDM/fSM6mI3kjdYHtujyEjqZfU/Eijy7dUhVrr6yxbiNCvmBXjqHtpKPxK2s6lg2Ayy0iWFTtlCUI27FQ9N3ib1kUUjAcS2jRMS37h7NdawWyndqfx9nPl4JiqNLwDCFniSyOiSfuApoUKnpwxEmmQVjS/b2DXP72fzQCXPqPn0/22JbPBWNTbfLjY2FWjAz0xKm2uWoyE59Fe7LeIKi5TDARhgx9uDm3FjG85NlZcuGkf8ykjhS70vNFoNCEX5urPrjf+i/HwbYp/xIb2GtxyYvpAIBYGbM+f5FVe7mx/voe61m13tbatNbJajlfBBYZ4n5cMDP/QusswbxayGRoVQ3ZObda+njVTP/SphXG4wB6ofuwLQ6gObpVrzHc2iPKdAfhp06JZm8KQiAVR2epl99VIu+KacXuA/pXH7FRqO7kb+QXLzOvsHG40XO+xDKPHjSkgq0+edq5pjR0wroNNNNIwsf13WQ== abuba@Abubakar"
)
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "oracle-ampere-vps")
SUBNET_ID = os.getenv(
    "SUBNET_ID",
    "ocid1.subnet.oc1.ap-mumbai-1.aaaaaaaaamoga7guxaxj2cuqg7oz2qz3xyqy6vvolltwq2wn3i47o476fyra"
)
IMAGE_ID = os.getenv(
    "IMAGE_ID",
    "ocid1.image.oc1.ap-mumbai-1.aaaaaaaa2jurqb3jm7hlpqw3au35loaqjkcozbyh7znn4jnfu5zbrgfulwia"
)
ARM_OCPUS = int(os.getenv("ARM_OCPUS", "4"))
ARM_MEMORY_IN_GBS = int(os.getenv("ARM_MEMORY_IN_GBS", "24"))
BOOT_VOLUME_SIZE_IN_GBS = int(os.getenv("BOOT_VOLUME_SIZE_IN_GBS", "150"))
RETRY_INTERVAL = int(os.getenv("RETRY_INTERVAL", "60"))
# ────────────────────────────────────────────────────────

config = oci.config.from_file()


def get_availability_domain():
    identity = oci.identity.IdentityClient(config)
    ads = identity.list_availability_domains(COMPARTMENT_ID).data
    return ads[0].name


def get_ubuntu_arm_image():
    if IMAGE_ID:
        return IMAGE_ID
    compute = oci.core.ComputeClient(config)
    images = compute.list_images(
        COMPARTMENT_ID,
        operating_system="Canonical Ubuntu",
        shape="VM.Standard.A1.Flex",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        raise Exception("Ubuntu ARM image not found")
    return images[0].id


def create_vcn_and_subnet():
    if SUBNET_ID:
        return SUBNET_ID
    network = oci.core.VirtualNetworkClient(config)

    # Check if VCN already exists
    vcns = network.list_vcns(COMPARTMENT_ID, display_name="retry-vcn").data
    if vcns:
        vcn = vcns[0]
        print(f"Using existing VCN: {vcn.id}")
    else:
        vcn = network.create_vcn(
            oci.core.models.CreateVcnDetails(
                compartment_id=COMPARTMENT_ID,
                display_name="retry-vcn",
                cidr_block="10.0.0.0/16",
            )
        ).data
        print(f"Created VCN: {vcn.id}")

        ig = network.create_internet_gateway(
            oci.core.models.CreateInternetGatewayDetails(
                compartment_id=COMPARTMENT_ID,
                vcn_id=vcn.id,
                display_name="retry-ig",
                is_enabled=True,
            )
        ).data

        network.update_route_table(
            vcn.default_route_table_id,
            oci.core.models.UpdateRouteTableDetails(
                route_rules=[
                    oci.core.models.RouteRule(
                        destination="0.0.0.0/0",
                        network_entity_id=ig.id,
                    )
                ]
            ),
        )

        security_lists = network.list_security_lists(COMPARTMENT_ID, vcn_id=vcn.id).data
        if security_lists:
            existing_egress = security_lists[0].egress_security_rules
            new_ingress = []
            for port in [22, 80, 443, 8501]:
                new_ingress.append(
                    oci.core.models.IngressSecurityRule(
                        protocol="6",
                        source="0.0.0.0/0",
                        tcp_options=oci.core.models.TcpOptions(
                            destination_port_range=oci.core.models.PortRange(
                                min=port, max=port
                            )
                        ),
                    )
                )
            network.update_security_list(
                security_lists[0].id,
                oci.core.models.UpdateSecurityListDetails(
                    ingress_security_rules=new_ingress,
                    egress_security_rules=existing_egress,
                ),
            )

    subnets = network.list_subnets(
        COMPARTMENT_ID, vcn_id=vcn.id, display_name="retry-subnet"
    ).data
    if subnets:
        subnet = subnets[0]
        print(f"Using existing subnet: {subnet.id}")
    else:
        subnet = network.create_subnet(
            oci.core.models.CreateSubnetDetails(
                compartment_id=COMPARTMENT_ID,
                vcn_id=vcn.id,
                display_name="retry-subnet",
                cidr_block="10.0.0.0/24",
                prohibit_public_ip_on_vnic=False,
            )
        ).data
        print(f"Created subnet: {subnet.id}")

    return subnet.id


def try_create_instance(subnet_id, ad_name, image_id):
    compute = oci.core.ComputeClient(config)
    instance = compute.launch_instance(
        oci.core.models.LaunchInstanceDetails(
            compartment_id=COMPARTMENT_ID,
            display_name=INSTANCE_NAME,
            availability_domain=ad_name,
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ARM_OCPUS,
                memory_in_gbs=ARM_MEMORY_IN_GBS,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=image_id,
                boot_volume_size_in_gbs=BOOT_VOLUME_SIZE_IN_GBS,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True,
            ),
            metadata={"ssh_authorized_keys": SSH_PUBLIC_KEY},
        )
    ).data
    return instance


def main():
    print("=" * 60)
    print("  Oracle Cloud Ampere A1 Auto-Retry (GitHub Actions / Cloud)")
    print(f"  Target: {ARM_OCPUS} OCPUs, {ARM_MEMORY_IN_GBS} GB RAM, {BOOT_VOLUME_SIZE_IN_GBS} GB Disk")
    print("=" * 60)

    print("Checking network configuration...")
    subnet_id = create_vcn_and_subnet()
    print(f"Subnet ID: {subnet_id}")

    print("Fetching availability domain...")
    ad_name = get_availability_domain()
    print(f"Availability Domain: {ad_name}")

    print("Checking Ubuntu ARM image...")
    image_id = get_ubuntu_arm_image()
    print(f"Image ID: {image_id}")

    attempt = 0
    while True:
        attempt += 1
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] Attempt #{attempt} to claim Ampere A1 instance...", flush=True)

        try:
            instance = try_create_instance(subnet_id, ad_name, image_id)
            print(f"\n✅ SUCCESS! Ampere A1 instance claimed!")
            print(f"   Instance ID: {instance.id}")
            print(f"   Lifecycle State: {instance.lifecycle_state}")
            print(f"   Instance Name: {instance.display_name}")
            break
        except oci.exceptions.ServiceError as e:
            if "Out of host capacity" in str(e) or "capacity" in str(e).lower():
                print(f"❌ Out of host capacity in {ad_name}. Retrying in {RETRY_INTERVAL}s...", flush=True)
            elif "TooManyRequests" in str(e) or e.status == 429:
                print(f"⚠️ Rate limited by Oracle (429). Pausing 75s...", flush=True)
                time.sleep(75)
            else:
                print(f"❌ Oracle API error: {e.message}. Retrying in {RETRY_INTERVAL}s...", flush=True)
        except Exception as e:
            print(f"⚠️ Connection timeout or network issue ({type(e).__name__}): {e}. Retrying in {RETRY_INTERVAL}s...", flush=True)

        time.sleep(RETRY_INTERVAL)


if __name__ == "__main__":
    main()
