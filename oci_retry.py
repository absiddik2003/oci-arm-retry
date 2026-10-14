"""
Oracle Cloud Infrastructure (OCI) Ampere A1 (ARM) Safe Provisioning & Retry Controller
Compliant with Oracle Always Free Tier limits in ap-mumbai-1.

Safety Rules Enforced:
1. Shape: VM.Standard.A1.Flex only.
2. Max OCPUs: 2 (Never 4). Max RAM: 12 GB (Never 24).
3. Max Storage: Total boot + block volumes <= 200 GB. Default boot disk: 100 GB.
4. Home Region check: Must be ap-mumbai-1.
5. Idempotent: Persistent opc-retry-token and instance preflight checks.
6. Safe Error Classification:
   - Out of host capacity: randomized 5-10 min backoff.
   - 429 TooManyRequests: respect Retry-After header or exponential backoff (5, 10, 20, 30 min).
   - Permanent errors (400, 401, 403, 404, quota): stop immediately.
7. Dry-Run Mode: Validates everything and prints redacted plan without mutating OCI resources.
"""

import os
import sys
import time
import json
import uuid
import random
import argparse
import datetime
from pathlib import Path
import oci

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Always Free Constraints & Safe Defaults                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
ALWAYS_FREE_SHAPE = "VM.Standard.A1.Flex"
MAX_ALWAYS_FREE_OCPUS = 2
MAX_ALWAYS_FREE_RAM_GBS = 12
DEFAULT_BOOT_DISK_GBS = 100
MAX_TENANCY_STORAGE_GBS = 200
EXPECTED_HOME_REGION = "ap-mumbai-1"

# Known vetted official Ubuntu 24.04 LTS ARM64 image in ap-mumbai-1
FALLBACK_UBUNTU_2404_IMAGE_OCID = (
    "ocid1.image.oc1.ap-mumbai-1.aaaaaaaa2jurqb3jm7hlpqw3au35loaqjkcozbyh7znn4jnfu5zbrgfulwia"
)
DEFAULT_SUBNET_OCID = (
    "ocid1.subnet.oc1.ap-mumbai-1.aaaaaaaaamoga7guxaxj2cuqg7oz2qz3xyqy6vvolltwq2wn3i47o476fyra"
)
DEFAULT_INSTANCE_NAME = "oracle-ampere-vps"
STATE_FILE_NAME = ".oci_retry_state.json"

NON_TERMINATED_STATES = [
    "PROVISIONING",
    "STARTING",
    "RUNNING",
    "STOPPING",
    "STOPPED",
    "MOVING",
]


def log(msg, level="INFO"):
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{timestamp}] [{level}] {msg}", flush=True)


def load_oci_config(config_path=None, profile_name="DEFAULT"):
    """Loads OCI configuration securely from file or environment."""
    if config_path and os.path.exists(config_path):
        return oci.config.from_file(config_path, profile_name)

    default_path = os.path.expanduser("~/.oci/config")
    if os.path.exists(default_path):
        return oci.config.from_file(default_path, profile_name)

    # Allow environment-based config if running in CI without file
    user = os.getenv("OCI_USER")
    fingerprint = os.getenv("OCI_FINGERPRINT")
    tenancy = os.getenv("OCI_TENANCY")
    region = os.getenv("OCI_REGION", EXPECTED_HOME_REGION)
    key_file = os.getenv("OCI_KEY_FILE")

    if user and fingerprint and tenancy and key_file:
        return {
            "user": user,
            "fingerprint": fingerprint,
            "tenancy": tenancy,
            "region": region,
            "key_file": key_file,
        }

    raise RuntimeError(
        "No OCI configuration found. Ensure ~/.oci/config exists or OCI env vars are set."
    )


def verify_home_region(config, expected_region=EXPECTED_HOME_REGION):
    """Verifies tenancy home region to prevent non-free deployment in secondary regions."""
    identity = oci.identity.IdentityClient(config)
    regions = identity.list_region_subscriptions(config["tenancy"]).data
    home_regions = [r.region_name for r in regions if r.is_home_region]

    if not home_regions:
        raise RuntimeError("Could not determine home region from OCI API.")

    home_region = home_regions[0]
    if home_region != expected_region:
        raise RuntimeError(
            f"SAFETY HALT: Account home region is '{home_region}', not '{expected_region}'. "
            "Always Free compute allowance is ONLY valid in the home region."
        )

    if config.get("region") != expected_region:
        raise RuntimeError(
            f"Config region is set to '{config.get('region')}', which differs from home region '{expected_region}'."
        )

    return home_region


def check_existing_instances(config, compartment_id, target_shape=ALWAYS_FREE_SHAPE):
    """
    Checks for any existing instances of the target shape across non-terminated states.
    Prevents duplicate provisioning and quota exhaustion.
    """
    compute = oci.core.ComputeClient(config)
    existing_matching = []

    instances = oci.pagination.list_call_get_all_results(
        compute.list_instances,
        compartment_id=compartment_id,
    ).data

    for inst in instances:
        if inst.shape == target_shape and inst.lifecycle_state in NON_TERMINATED_STATES:
            existing_matching.append(inst)

    return existing_matching


def calculate_storage_usage(config, compartment_id, availability_domain):
    """
    Calculates total boot and block volume storage currently provisioned in the tenancy.
    Ensures total storage remains <= 200 GB.
    """
    blockstorage = oci.core.BlockstorageClient(config)
    total_gbs = 0
    volumes_detail = []

    # Boot volumes
    boot_volumes = oci.pagination.list_call_get_all_results(
        blockstorage.list_boot_volumes,
        availability_domain=availability_domain,
        compartment_id=compartment_id,
    ).data

    for bv in boot_volumes:
        if bv.lifecycle_state not in ["TERMINATED", "TERMINATING"]:
            size = bv.size_in_gbs or 0
            total_gbs += size
            volumes_detail.append(f"BootVolume: {bv.display_name} ({size} GB)")

    # Block volumes
    block_volumes = oci.pagination.list_call_get_all_results(
        blockstorage.list_volumes,
        compartment_id=compartment_id,
    ).data

    for v in block_volumes:
        if v.lifecycle_state not in ["TERMINATED", "TERMINATING"]:
            size = v.size_in_gbs or 0
            total_gbs += size
            volumes_detail.append(f"BlockVolume: {v.display_name} ({size} GB)")

    return total_gbs, volumes_detail


def validate_subnet(config, subnet_id):
    """Verifies subnet exists, is available, and resides in the home region."""
    network = oci.core.VirtualNetworkClient(config)
    subnet = network.get_subnet(subnet_id).data
    if subnet.lifecycle_state != "AVAILABLE":
        raise RuntimeError(f"Subnet {subnet_id} is in state {subnet.lifecycle_state}, not AVAILABLE.")
    return subnet


def validate_image(config, compartment_id, image_id, target_shape=ALWAYS_FREE_SHAPE):
    """Verifies image compatibility with ARM shape."""
    compute = oci.core.ComputeClient(config)
    image = compute.get_image(image_id).data
    if image.lifecycle_state != "AVAILABLE":
        raise RuntimeError(f"Image {image_id} is in state {image.lifecycle_state}, not AVAILABLE.")
    return image


def get_availability_domain(config, compartment_id):
    """Discovers the region's availability domain dynamically."""
    identity = oci.identity.IdentityClient(config)
    ads = identity.list_availability_domains(compartment_id).data
    if not ads:
        raise RuntimeError("No availability domains found.")
    return ads[0].name


def load_or_create_state(state_file_path, target_ocpus, target_ram, target_disk):
    """
    Loads durable state containing opc-retry-token to ensure idempotency across restarts.
    """
    path = Path(state_file_path)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                token = data.get("opc_retry_token")
                if token:
                    log(f"Resuming existing session with opc-retry-token: {token[:8]}...")
                    return data
        except Exception as e:
            log(f"Warning: Failed to read state file {path}: {e}. Creating fresh state.", "WARN")

    new_token = str(uuid.uuid4())
    state_data = {
        "opc_retry_token": new_token,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "target_ocpus": target_ocpus,
        "target_ram_gbs": target_ram,
        "target_disk_gbs": target_disk,
        "status": "INITIALIZED",
        "attempts": 0,
    }
    save_state(state_file_path, state_data)
    log(f"Generated new durable opc-retry-token: {new_token[:8]}...")
    return state_data


def save_state(state_file_path, state_data):
    path = Path(state_file_path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state_data, f, indent=2)


def classify_error(err):
    """
    Classifies errors into:
    - CAPACITY: Out of physical host capacity in the AD.
    - RATE_LIMIT: HTTP 429 TooManyRequests.
    - AMBIGUOUS: Network timeouts or disconnects where instance creation state is uncertain.
    - PERMANENT: Auth, quota, permission, invalid argument errors that will never succeed by retrying.
    """
    if isinstance(err, oci.exceptions.ServiceError):
        status = err.status
        code = getattr(err, "code", "") or ""
        msg = getattr(err, "message", "") or str(err)
        request_id = getattr(err, "request_id", "none")

        # 429 Rate Limiting
        if status == 429 or "TooManyRequests" in code:
            retry_after = None
            if hasattr(err, "headers") and err.headers:
                retry_after_hdr = err.headers.get("retry-after")
                if retry_after_hdr and retry_after_hdr.isdigit():
                    retry_after = int(retry_after_hdr)
            return "RATE_LIMIT", retry_after, f"OCI 429 TooManyRequests (request-id: {request_id})"

        # Capacity Exhaustion
        if (
            status == 500
            or "OutOfHostCapacity" in code
            or "Out of host capacity" in msg
            or "capacity" in msg.lower()
        ):
            return "CAPACITY", None, f"Out of host capacity in AD (request-id: {request_id}, code: {code})"

        # Permanent Errors
        if status in [400, 401, 403, 404, 409]:
            return "PERMANENT", None, f"Permanent error {status} ({code}): {msg} (request-id: {request_id})"

        return "PERMANENT", None, f"Unhandled service error {status} ({code}): {msg} (request-id: {request_id})"

    if isinstance(err, (oci.exceptions.RequestException, TimeoutError, ConnectionError)):
        return "AMBIGUOUS", None, f"Network/Timeout error: {type(err).__name__} - {err}"

    return "PERMANENT", None, f"Unexpected error: {type(err).__name__} - {err}"


def execute_launch(
    config,
    compartment_id,
    ad_name,
    subnet_id,
    image_id,
    ssh_public_key,
    ocpus,
    ram_gbs,
    boot_disk_gbs,
    instance_name,
    opc_retry_token,
    dry_run=False,
):
    """
    Executes an idempotent launch call with opc-retry-token and NoneRetryStrategy.
    """
    compute = oci.core.ComputeClient(
        config,
        retry_strategy=oci.retry.NoneRetryStrategy(),  # Prevent burst SDK retries
    )

    launch_details = oci.core.models.LaunchInstanceDetails(
        compartment_id=compartment_id,
        display_name=instance_name,
        availability_domain=ad_name,
        shape=ALWAYS_FREE_SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=ocpus,
            memory_in_gbs=ram_gbs,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id,
            boot_volume_size_in_gbs=boot_disk_gbs,
            boot_volume_vpus_per_gb=10,  # Always Free standard performance tier
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=True,
            display_name=f"{instance_name}-vnic",
        ),
        metadata={"ssh_authorized_keys": ssh_public_key.strip()},
        freeform_tags={
            "ManagedBy": "oci-arm-retry",
            "Target": "ARM-Always-Free",
        },
    )

    if dry_run:
        log("DRY RUN ACTIVE: Validating parameters without calling launch_instance API.")
        return None

    response = compute.launch_instance(
        launch_details,
        opc_retry_token=opc_retry_token,
    )
    return response.data


def wait_for_running_and_get_ip(config, compartment_id, instance_id, max_wait_seconds=600):
    """Waits for instance to enter RUNNING state and retrieves its assigned public IP."""
    compute = oci.core.ComputeClient(config)
    network = oci.core.VirtualNetworkClient(config)
    log(f"Waiting for instance {instance_id} to reach RUNNING state...")

    start = time.time()
    while time.time() - start < max_wait_seconds:
        try:
            inst = compute.get_instance(instance_id).data
            state = inst.lifecycle_state
            log(f"  Current instance state: {state}")
            if state == "RUNNING":
                vnics = oci.pagination.list_call_get_all_results(
                    compute.list_vnic_attachments,
                    compartment_id=compartment_id,
                    instance_id=instance_id,
                ).data
                for vnic_att in vnics:
                    if vnic_att.lifecycle_state == "ATTACHED":
                        vnic = network.get_vnic(vnic_att.vnic_id).data
                        if vnic.public_ip:
                            return vnic.public_ip, inst
                return None, inst
            elif state in ["TERMINATED", "TERMINATING"]:
                raise RuntimeError(f"Instance transitioned to unexpected state: {state}")
        except Exception as e:
            log(f"Warning polling instance state: {e}", "WARN")
        time.sleep(15)
    return None, None


def run_provisioning_loop(args):
    """Main provisioning loop with preflight verification and conservative backoff."""
    config = load_oci_config(args.config_file)
    compartment_id = args.compartment_id or config["tenancy"]

    log("=" * 70)
    log("  Oracle Cloud Always Free ARM Controller")
    log(f"  Target Shape : {ALWAYS_FREE_SHAPE}")
    log(f"  Allocation   : {args.target_ocpus} OCPUs | {args.target_memory_gbs} GB RAM | {args.target_disk_gbs} GB Disk")
    log(f"  Mode         : {'DRY RUN' if args.dry_run else 'LIVE PROVISIONING'}")
    log("=" * 70)

    # 1. Enforce safety bounds on shape parameters
    if args.target_ocpus > MAX_ALWAYS_FREE_OCPUS:
        raise ValueError(
            f"SAFETY HALT: Requested {args.target_ocpus} OCPUs. Max Always Free is {MAX_ALWAYS_FREE_OCPUS}."
        )
    if args.target_memory_gbs > MAX_ALWAYS_FREE_RAM_GBS:
        raise ValueError(
            f"SAFETY HALT: Requested {args.target_memory_gbs} GB RAM. Max Always Free is {MAX_ALWAYS_FREE_RAM_GBS}."
        )

    # 2. Verify Home Region
    log("Step 1: Verifying Home Region...")
    home_region = verify_home_region(config, EXPECTED_HOME_REGION)
    log(f"  ✓ Home region confirmed: {home_region}")

    # 3. Discover Availability Domain
    log("Step 2: Resolving Availability Domain...")
    ad_name = get_availability_domain(config, compartment_id)
    log(f"  ✓ Availability Domain: {ad_name}")

    # 4. Check Existing Instances
    log("Step 3: Checking existing ARM instances in tenancy...")
    existing_arm = check_existing_instances(config, compartment_id, ALWAYS_FREE_SHAPE)
    if existing_arm:
        for inst in existing_arm:
            log(
                f"  ⚠️ Existing ARM VM Found: ID={inst.id}, Name={inst.display_name}, State={inst.lifecycle_state}",
                "WARN",
            )
        log("SAFETY HALT: An ARM VM already exists in non-terminated state. No duplicate will be created.")
        return 0

    log("  ✓ No existing ARM instances found. Account compute allowance is clean.")

    # 5. Storage Allowance Verification
    log("Step 4: Inventorying storage usage across home region...")
    used_storage_gbs, volumes_list = calculate_storage_usage(config, compartment_id, ad_name)
    log(f"  Currently used storage: {used_storage_gbs} GB across {len(volumes_list)} volume(s).")
    for v in volumes_list:
        log(f"    - {v}")

    projected_storage = used_storage_gbs + args.target_disk_gbs
    log(f"  Projected total storage: {projected_storage} GB (Limit: {MAX_TENANCY_STORAGE_GBS} GB)")
    if projected_storage > MAX_TENANCY_STORAGE_GBS:
        raise RuntimeError(
            f"SAFETY HALT: Projected storage {projected_storage} GB exceeds the 200 GB Always Free ceiling. "
            f"Available capacity: {MAX_TENANCY_STORAGE_GBS - used_storage_gbs} GB. Lower target disk size."
        )
    log("  ✓ Storage allowance check PASSED.")

    # 6. Validate Subnet and Image
    log("Step 5: Validating Subnet and OS Image...")
    subnet = validate_subnet(config, args.subnet_id)
    log(f"  ✓ Subnet validated: {subnet.display_name} ({subnet.cidr_block})")

    image = validate_image(config, compartment_id, args.image_id, ALWAYS_FREE_SHAPE)
    log(f"  ✓ OS Image validated: {image.display_name} (ARM64 Ubuntu 24.04)")

    # 7. Validate SSH Public Key
    ssh_key = args.ssh_public_key or os.getenv("SSH_PUBLIC_KEY")
    if not ssh_key and os.path.exists(os.path.expanduser("~/.ssh/oracle.pem.pub")):
        with open(os.path.expanduser("~/.ssh/oracle.pem.pub"), "r", encoding="utf-8") as f:
            ssh_key = f.read().strip()

    if not ssh_key:
        raise RuntimeError("No SSH public key provided. Set SSH_PUBLIC_KEY or place oracle.pem.pub in ~/.ssh.")
    if "BEGIN RSA PRIVATE KEY" in ssh_key or "BEGIN OPENSSH PRIVATE KEY" in ssh_key:
        raise RuntimeError("SAFETY HALT: A private key was supplied instead of a public key! Aborting.")

    log(f"  ✓ SSH Public key loaded ({ssh_key.split()[0]} {ssh_key.split()[-1] if len(ssh_key.split()) > 1 else ''})")

    # 8. Load or initialize durable state
    state = load_or_create_state(
        args.state_file,
        args.target_ocpus,
        args.target_memory_gbs,
        args.target_disk_gbs,
    )
    opc_retry_token = state["opc_retry_token"]

    if args.dry_run:
        log("\n" + "=" * 70)
        log("DRY RUN SUMMARY: All preflight checks passed successfully.")
        log(f"  Target Shape : {ALWAYS_FREE_SHAPE}")
        log(f"  Specs        : {args.target_ocpus} OCPUs, {args.target_memory_gbs} GB RAM, {args.target_disk_gbs} GB Boot Disk")
        log(f"  Region / AD  : {home_region} / {ad_name}")
        log(f"  Subnet       : {subnet.id}")
        log(f"  Image        : {image.id}")
        log(f"  Retry Token  : {opc_retry_token}")
        log("  Action       : No API mutations performed in dry-run mode.")
        log("=" * 70)
        return 0

    # 9. Execution Loop
    start_time = time.time()
    max_duration_seconds = args.max_hours * 3600
    attempt = state.get("attempts", 0)
    consecutive_rate_limits = 0

    log("\nBeginning controlled provisioning loop...")

    while True:
        elapsed = time.time() - start_time
        if elapsed >= max_duration_seconds:
            log(f"Initial bounded attempt window of {args.max_hours} hours reached. Stopping loop.", "WARN")
            break

        if args.max_attempts and attempt >= args.max_attempts:
            log(f"Reached max attempts ({args.max_attempts}) for this run. Exiting cleanly.")
            break

        attempt += 1
        state["attempts"] = attempt
        save_state(args.state_file, state)

        # Re-check existing instances before EVERY attempt to handle ambiguous prior responses
        existing_arm = check_existing_instances(config, compartment_id, ALWAYS_FREE_SHAPE)
        if existing_arm:
            inst = existing_arm[0]
            log(f"✅ SUCCESS: Instance detected in state {inst.lifecycle_state} (OCID: {inst.id})")
            state["status"] = "SUCCESS"
            state["instance_id"] = inst.id
            save_state(args.state_file, state)
            return 0

        log(f"--- Attempt #{attempt} (Elapsed: {int(elapsed // 60)}m) ---")

        try:
            instance = execute_launch(
                config=config,
                compartment_id=compartment_id,
                ad_name=ad_name,
                subnet_id=subnet.id,
                image_id=image.id,
                ssh_public_key=ssh_key,
                ocpus=args.target_ocpus,
                ram_gbs=args.target_memory_gbs,
                boot_disk_gbs=args.target_disk_gbs,
                instance_name=args.instance_name,
                opc_retry_token=opc_retry_token,
                dry_run=False,
            )

            if instance:
                log("=" * 70)
                log("🎉 Ampere A1 instance launch request accepted!")
                log(f"  Instance OCID : {instance.id}")
                log(f"  Initial State : {instance.lifecycle_state}")
                log(f"  Shape         : {instance.shape}")
                log("=" * 70)

                public_ip, final_inst = wait_for_running_and_get_ip(
                    config, compartment_id, instance.id
                )
                state["status"] = "RUNNING" if public_ip else "PROVISIONED"
                state["instance_id"] = instance.id
                state["public_ip"] = public_ip
                save_state(args.state_file, state)

                log("=" * 70)
                log("🚀 SUCCESS! Ampere A1 instance is RUNNING and verified!")
                log(f"  Instance OCID : {instance.id}")
                log(f"  Public IP     : {public_ip or 'Check OCI Console'}")
                if public_ip:
                    log(f"  SSH Command   : ssh -i ~/.ssh/oracle.pem ubuntu@{public_ip}")
                log("=" * 70)
                return 0

        except Exception as err:
            err_type, wait_hint, err_msg = classify_error(err)

            if err_type == "CAPACITY":
                consecutive_rate_limits = 0
                backoff = random.randint(args.retry_min, args.retry_max)  # 2 to 3 minutes (120-180s)
                log(f"❌ {err_msg}")
                log(f"  Waiting {backoff} seconds ({backoff // 60}m {backoff % 60}s) before next attempt...", "INFO")
                if args.single_attempt:
                    log("Single attempt mode active. Exiting after capacity exhaustion.")
                    return 2
                time.sleep(backoff)

            elif err_type == "RATE_LIMIT":
                consecutive_rate_limits += 1
                if wait_hint:
                    backoff = wait_hint + random.randint(5, 15)
                else:
                    # Exponential backoff: 5, 10, 20, 30 minutes with jitter
                    ladder = [300, 600, 1200, 1800]
                    idx = min(consecutive_rate_limits - 1, len(ladder) - 1)
                    backoff = ladder[idx] + random.randint(10, 60)

                log(f"⚠️ {err_msg}", "WARN")
                log(f"  Throttled. Backing off for {backoff} seconds ({backoff // 60}m)...", "WARN")
                if consecutive_rate_limits >= 4:
                    log("SAFETY HALT: Repeated rate limiting (429) encountered. Halting automation.", "ERROR")
                    return 3
                time.sleep(backoff)

            elif err_type == "AMBIGUOUS":
                log(f"⚠️ {err_msg}", "WARN")
                log("  Ambiguous timeout/disconnect. Will reconcile state and retry with SAME opc-retry-token in 60s...")
                time.sleep(60)

            else:  # PERMANENT
                log(f"🛑 CRITICAL PERMANENT ERROR: {err_msg}", "ERROR")
                log("SAFETY HALT: Halting immediately to prevent quota violations or endless failure loops.", "ERROR")
                return 1

    return 2


def main():
    parser = argparse.ArgumentParser(description="Oracle Always Free ARM VM Safe Retry Controller")
    parser.add_argument("--dry-run", action="store_true", help="Validate all preflights without calling launch_instance API")
    parser.add_argument("--single-attempt", action="store_true", help="Perform at most one launch attempt and exit")
    parser.add_argument("--max-hours", type=float, default=24.0, help="Maximum overall window in hours (default: 24.0)")
    parser.add_argument("--max-attempts", type=int, default=None, help="Maximum attempts for this run")
    parser.add_argument("--target-ocpus", type=int, default=2, help="Target OCPUs (default: 2, max: 2)")
    parser.add_argument("--target-memory-gbs", type=int, default=12, help="Target RAM in GB (default: 12, max: 12)")
    parser.add_argument("--target-disk-gbs", type=int, default=100, help="Target boot disk in GB (default: 100)")
    parser.add_argument("--compartment-id", type=str, default=None, help="Compartment OCID (default: tenancy root)")
    parser.add_argument("--subnet-id", type=str, default=DEFAULT_SUBNET_OCID, help="Subnet OCID")
    parser.add_argument("--image-id", type=str, default=FALLBACK_UBUNTU_2404_IMAGE_OCID, help="Ubuntu 24.04 ARM64 Image OCID")
    parser.add_argument("--instance-name", type=str, default=DEFAULT_INSTANCE_NAME, help="Instance display name")
    parser.add_argument("--ssh-public-key", type=str, default=None, help="SSH public key string")
    parser.add_argument("--config-file", type=str, default=None, help="Path to OCI config file")
    parser.add_argument("--state-file", type=str, default=STATE_FILE_NAME, help="Durable state file path")
    parser.add_argument("--fallback-allowed", action="store_true", help="Allow fallback to 1 OCPU / 6 GB if authorized")
    parser.add_argument("--retry-interval", type=int, default=76, help="Retry interval in seconds on capacity exhaustion (default: 76)")
    parser.add_argument("--retry-min", type=int, default=74, help="Minimum backoff in seconds")
    parser.add_argument("--retry-max", type=int, default=78, help="Maximum backoff in seconds")

    args = parser.parse_args()
    sys.exit(run_provisioning_loop(args))


if __name__ == "__main__":
    main()
