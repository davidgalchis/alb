import boto3
import botocore
# import jsonschema
import json
import traceback
import zipfile
import os
import subprocess
import logging

from botocore.exceptions import ClientError

from extutil import remove_none_attributes, account_context, ExtensionHandler, ext, \
    current_epoch_time_usec_num, component_safe_name, lambda_env, random_id, \
    handle_common_errors

eh = ExtensionHandler()

# Import your clients
client = boto3.client('elbv2')

logger = logging.getLogger()
logger.setLevel(logging.INFO)

"""
eh calls
    eh.add_op() call MUST be made for the function to execute! Adds functions to the execution queue.
    eh.add_props() is used to add useful bits of information that can be used by this component or other components to integrate with this.
    eh.add_links() is used to add useful links to the console, the deployed infrastructure, the logs, etc that pertain to this component.
    eh.retry_error(a_unique_id_for_the_error(if you don't want it to fail out after 6 tries), progress=65, callback_sec=8)
        This is how to wait and try again
        Only set callback seconds for a wait, not an error
        @ext() runs the function if its operation is present and there isn't already a retry declared
    eh.add_log() is how logs are passed to the front-end
    eh.perm_error() is how you permanently fail the component deployment and send a useful message to the front-end
    eh.finish() just finishes the deployment and sends back message and progress
    *RARE*
    eh.add_state() takes a dictionary, merges existing with new
        This is specifically if CloudKommand doesn't need to store it for later. Thrown away at the end of the deployment.
        Preserved across retries, but not across deployments.
There are three elements of state preserved across retries:
    - eh.props
    - eh.links 
    - eh.state 
Wrap all operations you want to run with the following:
    @ext(handler=eh, op="your_operation_name")
Progress only needs to be explicitly reported on 1) a retry 2) an error. Finishing auto-sets progress to 100. 
"""

def safe_cast(val, to_type, default=None):
    try:
        return to_type(val)
    except (ValueError, TypeError):
        return default

def lambda_handler(event, context):
    try:
        # All relevant data is generally in the event, excepting the region and account number
        print(f"event = {event}")
        region = account_context(context)['region']
        account_number = account_context(context)['number']

        # This copies the operations, props, links, retry data, and remaining operations that are sent from CloudKommand. 
        # Just always include this.
        eh.capture_event(event)

        # These are other important values you will almost always use
        prev_state = event.get("prev_state") or {}
        project_code = event.get("project_code")
        repo_id = event.get("repo_id")
        cdef = event.get("component_def")
        cname = event.get("component_name")
        
        # Generate or read from component definition the identifier / name of the component here 
        name = eh.props.get("name") or cdef.get("name") or component_safe_name(project_code, repo_id, cname, no_underscores=True, no_uppercase=True, max_chars=32)

        # you pull in whatever arguments you care about
        """
        # Some examples. S3 doesn't really need any because each attribute is a separate call. 
        auto_verified_attributes = cdef.get("auto_verified_attributes") or ["email"]
        alias_attributes = cdef.get("alias_attributes") or ["preferred_username", "phone_number", "email"]
        username_attributes = cdef.get("username_attributes") or None
        """

        ### ATTRIBUTES THAT CAN BE SET ON INITIAL CREATION
        subnets = cdef.get('subnets')
        # Removing subnet mappings for the time being to decrease complexity, since you cannot specify both subnets and subnet mappings
        # subnet_mappings = cdef.get('subnet_mappings') # Cannot specify both subnets and subnet mappings
        security_groups = cdef.get('security_groups')
        scheme = cdef.get('scheme') or "internal"
        tags = cdef.get('tags') # this is converted to a [{"Key": key, "Value": value} , ...] format
        load_balancer_type = cdef.get('load_balancer_type') or "application"
        ip_address_type = cdef.get('ip_address_type') or 'ipv4'
        # customer_owned_ipv4_pool = cdef.get("customer_owned_ipv4_pool") # To be added once AWS Outpost support is added


        ### SPECIAL ATTRIBUTES THAT CAN ONLY BE ADDED POST INITIAL CREATION
        # supported by all load balancers
        deletion_protection_enabled = cdef.get("deletion_protection_enabled")
        load_balancing_cross_zone_enabled = cdef.get("load_balancing_cross_zone_enabled")
        # supported by both Application Load Balancers and Network Load Balancers
        access_logs_s3_enabled = cdef.get("access_logs_s3_enabled")
        access_logs_s3_bucket = cdef.get("access_logs_s3_bucket")
        access_logs_s3_prefix = cdef.get("access_logs_s3_prefix")
        ipv6_deny_all_igw_traffic = cdef.get("ipv6_deny_all_igw_traffic")
        # supported by only Application Load Balancers
        idle_timeout_timeout_seconds = cdef.get("idle_timeout_timeout_seconds")
        routing_http_desync_mitigation_mode = cdef.get("routing_http_desync_mitigation_mode")
        routing_http_drop_invalid_header_fields_enabled = cdef.get("routing_http_drop_invalid_header_fields_enabled")
        routing_http_preserve_host_header_enabled = cdef.get("routing_http_preserve_host_header_enabled")
        routing_http_x_amzn_tls_version_and_cipher_suite_enabled = cdef.get("routing_http_x_amzn_tls_version_and_cipher_suite_enabled")
        routing_http_xff_client_port_enabled = cdef.get("routing_http_xff_client_port_enabled")
        routing_http_xff_header_processing_mode = cdef.get("routing_http_xff_header_processing_mode")
        routing_http2_enabled = cdef.get("routing_http2_enabled")
        waf_fail_open_enabled = cdef.get("waf_fail_open_enabled")

        special_attributes = remove_none_attributes({
            "deletion_protection.enabled": str(deletion_protection_enabled).lower() if deletion_protection_enabled else deletion_protection_enabled,
            "load_balancing.cross_zone.enabled": str(load_balancing_cross_zone_enabled).lower() if load_balancing_cross_zone_enabled else load_balancing_cross_zone_enabled,
            "access_logs.s3.enabled": str(access_logs_s3_enabled).lower() if access_logs_s3_enabled else access_logs_s3_enabled,
            "access_logs.s3.bucket": str(access_logs_s3_bucket) if access_logs_s3_bucket else access_logs_s3_bucket,
            "access_logs.s3.prefix": str(access_logs_s3_prefix) if access_logs_s3_prefix else access_logs_s3_prefix,
            "ipv6.deny_all_igw_traffic": str(ipv6_deny_all_igw_traffic) if ipv6_deny_all_igw_traffic else ipv6_deny_all_igw_traffic,
            "idle_timeout.timeout_seconds": str(idle_timeout_timeout_seconds) if idle_timeout_timeout_seconds else idle_timeout_timeout_seconds,
            "routing.http.desync_mitigation_mode": str(routing_http_desync_mitigation_mode) if routing_http_desync_mitigation_mode else routing_http_desync_mitigation_mode,
            "routing.http.drop_invalid_header_fields.enabled": str(routing_http_drop_invalid_header_fields_enabled).lower() if routing_http_drop_invalid_header_fields_enabled else routing_http_drop_invalid_header_fields_enabled,
            "routing.http.preserve_host_header.enabled": str(routing_http_preserve_host_header_enabled).lower() if routing_http_preserve_host_header_enabled else routing_http_preserve_host_header_enabled,
            "routing.http.x_amzn_tls_version_and_cipher_suite.enabled": str(routing_http_x_amzn_tls_version_and_cipher_suite_enabled).lower() if routing_http_x_amzn_tls_version_and_cipher_suite_enabled else routing_http_x_amzn_tls_version_and_cipher_suite_enabled,
            "routing.http.xff_client_port.enabled": str(routing_http_xff_client_port_enabled).lower() if routing_http_xff_client_port_enabled else routing_http_xff_client_port_enabled,
            "routing.http.xff_header_processing.mode": str(routing_http_xff_header_processing_mode) if routing_http_xff_header_processing_mode else routing_http_xff_header_processing_mode,
            "routing.http2.enabled": str(routing_http2_enabled).lower() if routing_http2_enabled else routing_http2_enabled,
            "waf.fail_open.enabled": str(waf_fail_open_enabled).lower() if waf_fail_open_enabled else waf_fail_open_enabled
        })

        default_special_attributes = remove_none_attributes({
            "deletion_protection.enabled": "false",
            "load_balancing.cross_zone.enabled": "true",
            "access_logs.s3.enabled": "false",
            "access_logs.s3.bucket": None,
            "access_logs.s3.prefix": None,
            "ipv6.deny_all_igw_traffic": "false",
            "idle_timeout.timeout_seconds": "60",
            "routing.http.desync_mitigation_mode": "defensive",
            "routing.http.drop_invalid_header_fields.enabled": "false",
            "routing.http.preserve_host_header.enabled": "false",
            "routing.http.x_amzn_tls_version_and_cipher_suite.enabled": "false",
            "routing.http.xff_client_port.enabled": "false",
            "routing.http.xff_header_processing.mode": "append",
            "routing.http2.enabled": "true",
            "waf.fail_open.enabled": "false"
        })
            
        # remove any None values from the attributes dictionary
        attributes = remove_none_attributes({
            "Name": name,
            "Subnets": subnets,
            # "SubnetMappings": subnet_mappings, # removed for the time being to reduce complexity for the user
            "SecurityGroups": security_groups,
            "Scheme": scheme,
            "Tags": [{"Key": f"{key}", "Value": f"{value}"} for key, value in tags.items()] if tags else None,
            "Type": load_balancer_type,
            "IpAddressType": ip_address_type,
            # "CustomerOwnedIpv4Pool": customer_owned_ipv4_pool # To be added once AWS Outpost support is added
        })

        ### DECLARE STARTING POINT
        pass_back_data = event.get("pass_back_data", {}) # pass_back_data only exists if this is a RETRY
        # If a RETRY, then don't set starting point
        if pass_back_data:
            pass # If pass_back_data exists, then eh has already loaded in all relevant RETRY information.
        # If NOT retrying, and we are instead upserting, then we start with the GET STATE call
        elif event.get("op") == "upsert":

            old_name = None
            old_scheme = None
            old_load_balancer_type = None
            # old_customer_owned_ipv4_pool = None # To be added once AWS Outpost support is added

            try:
                old_name = prev_state["props"]["name"]
                old_scheme = prev_state["props"]["scheme"]
                old_load_balancer_type = prev_state["props"]["load_balancer_type"]
                # old_customer_owned_ipv4_pool = prev_state["props"]["customer_owned_ipv4_pool"] # To be added once AWS Outpost support is added
            except:
                pass

            eh.add_op("get_load_balancer")

            # If any non-editable fields have changed, we are choosing to fail. 
            # We are NOT choosing to delete and recreate because a listener may be attached and that MUST be removed before the load balancer can be deleted. 
            # Therefore a switchover is necessary to change un-editable values.
            if (old_name and old_name != name) or \
                (old_scheme and old_scheme != scheme) or \
                (old_load_balancer_type and old_load_balancer_type != load_balancer_type):

                non_editable_error_message = "You may not edit the name, scheme, or load balancer type of an existing load balancer. Please create a new component to get the desired configuration."
                eh.add_log("Cannot edit non-editable field", {"error": non_editable_error_message}, is_error=True)
                eh.perm_error(non_editable_error_message, 10)

        # If NOT retrying, and we are instead deleting, then we start with the DELETE call 
        #   (sometimes you start with GET STATE if you need to make a call for the identifier)
        elif event.get("op") == "delete":
            eh.add_op("delete_load_balancer")
            eh.add_state({"load_balancer_arn": prev_state["props"]["arn"]})

        # The ordering of call declarations should generally be in the following order
        # GET STATE
        # CREATE
        # UPDATE
        # DELETE
        # GENERATE PROPS
        
        ### The section below DECLARES all of the calls that can be made. 
        ### The eh.add_op() function MUST be called for actual execution of any of the functions. 

        ### GET STATE
        get_load_balancer(name, attributes, special_attributes, default_special_attributes, region, prev_state)

        ### DELETE CALL(S)
        delete_load_balancer()

        ### CREATE CALL(S) (occasionally multiple)
        create_load_balancer(attributes, special_attributes, default_special_attributes, region, prev_state)
        check_load_balancer_create_complete()
        
        ### UPDATE CALLS (common to have multiple)
        # You want ONE function per boto3 update call, so that retries come back to the EXACT same spot. 
        remove_tags()
        set_tags()
        set_ip_address_type(ip_address_type)
        set_security_groups(security_groups)
        set_subnets(subnets, ip_address_type, load_balancer_type)
        update_load_balancer_special_attributes()
        reset_load_balancer_special_attributes(default_special_attributes)

        ### GENERATE PROPS (sometimes can be done in get/create)

        # IMPORTANT! ALWAYS include this. Sends back appropriate data to CloudKommand.
        return eh.finish()

    # You want this. Leave it.
    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()

### GET STATE
# ALWAYS put the ext decorator on ALL calls that are referenced above
# This is ONLY called when this operation is slated to occur.
# GENERALLY, this function will make a bunch of eh.add_op() calls which determine what actions will be executed.
#   The eh.add_op() call MUST be made for the function to execute!
# eh.add_props() is used to add useful bits of information that can be used by this component or other components to integrate with this.
# eh.add_links() is used to add useful links to the console, the deployed infrastructure, the logs, etc that pertain to this component.
@ext(handler=eh, op="get_load_balancer")
def get_load_balancer(name, attributes, special_attributes, default_special_attributes, region, prev_state):
    client = boto3.client("elbv2")

    if prev_state and prev_state.get("props") and prev_state.get("props").get("name"):
        prev_name = prev_state.get("props").get("name")
        if name != prev_name:
            eh.perm_error("Cannot Change Load Balancer Name", progress=0)
            return None
    
    # Try to get the load balancer. If you succeed, record the props and links from the current load balancer
    try:
        response = client.describe_load_balancers(Names=[name])
        load_balancer_to_use = None
        load_balancer_arn = None
        if response and response.get("LoadBalancers") and len(response.get("LoadBalancers")) > 0:
            eh.add_log("Got Load Balancer Attributes", response)
            load_balancer_to_use = response.get("LoadBalancers")[0]
            load_balancer_arn = load_balancer_to_use.get("LoadBalancerArn")
            eh.add_state({"load_balancer_arn": load_balancer_to_use.get("LoadBalancerArn"), "region": region})
            eh.add_props({
                "name": name,
                "arn": load_balancer_to_use.get("LoadBalancerArn"),
                "dns_name": load_balancer_to_use.get("DNSName"),
                "canonical_hosted_zone_id": load_balancer_to_use.get("CanonicalHostedZoneId"),
                "vpc_id": load_balancer_to_use.get("VpcId"),
                "load_balancer_type": load_balancer_to_use.get("Type"),
                "security_groups": load_balancer_to_use.get("SecurityGroups"),
                "subnets": [item.get("SubnetId") for item in load_balancer_to_use.get("AvailabilityZones", [])],
                "scheme": load_balancer_to_use.get("Scheme"),
                "ip_address_type": load_balancer_to_use.get("IpAddressType")
            })
            eh.add_links({"Load Balancer": gen_load_balancer_link(region, load_balancer_arn)})


            ### If the load_balancer exists, then setup any followup tasks
            
            # Set the load balancer ip address type and subnets
            if attributes.get("ip_address_type") != prev_state.get("props", {}).get("ip_address_type"):
                # Set the load balancer ip address type
                eh.add_op("set_ip_address_type")
            # Set the load balancer security groups (and ip address type, if network load balancer)
            if attributes.get("subnets") != prev_state.get("props", {}).get("subnets") or \
                (attributes.get("ip_address_type") != prev_state.get("props", {}).get("ip_address_type") and attributes["load_balancer_type"] == "network"):
                eh.add_op("set_subnets")
            # Set the load balancer security groups
            if attributes.get("security_groups") != prev_state.get("props", {}).get("security_groups"):
                eh.add_op("set_security_groups")
            
            # Figure out if there are special attributes that need to be set, otherwise reset all of the special attributes that exist current on the load balancer to their original values
            try:
                
                # Try to get the current special attributes
                response = client.describe_load_balancer_attributes(LoadBalancerArn=load_balancer_arn)
                eh.add_log("Got Load Balancer Special Attributes", response)
                current_special_attributes = {item.get("Key"): item.get("Value") for item in response["Attributes"]}
                if load_balancer_to_use.get("IpAddressType") == "ipv4":
                    current_special_attributes.pop("ipv6.deny_all_igw_traffic", None)

                if special_attributes:
                    # You either use whatever is being passed in or the default. That's it. And you only care about parameters that are allowed to be set for the current setup.
                    update_special_attributes = remove_none_attributes({key: (special_attributes.get(key) or default_special_attributes.get(key)) for key, current_value in current_special_attributes.items() })
                    eh.add_state({"update_special_attributes": update_special_attributes})
                    eh.add_op("update_load_balancer_special_attributes", update_special_attributes)
                else:
                    eh.add_state({"current_special_attributes": current_special_attributes})
                    eh.add_op("reset_load_balancer_special_attributes")
            # If the load balancer does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
            except client.exceptions.LoadBalancerNotFoundException:
                eh.add_log("Load Balancer Not Found", {"arn": load_balancer_arn})
                pass

            try:
                # Try to get the current tags
                response = client.describe_tags(ResourceArns=[load_balancer_arn])
                eh.add_log("Got Tags")
                relevant_items = [item for item in response.get("TagDescriptions") if item.get("ResourceArn") == load_balancer_arn]
                current_tags = {}

                # Parse out the current tags
                if len(relevant_items) > 0:
                    relevant_item = relevant_items[0]
                    if relevant_item.get("Tags"):
                        current_tags = {item.get("Key") : item.get("Value") for item in relevant_item.get("Tags")}

                # If there are tags specified, figure out which ones need to be added and which ones need to be removed
                if attributes.get("Tags"):

                    tags = attributes.get("Tags")
                    formatted_tags = {item.get("Key") : item.get("Value") for item in tags}
                    # Compare the current tags to the desired tags
                    if formatted_tags != current_tags:
                        remove_tags = [k for k in current_tags.keys() if k not in formatted_tags]
                        add_tags = {k:v for k,v in formatted_tags.items() if v != current_tags.get(k)}
                        if remove_tags:
                            eh.add_op("remove_tags", remove_tags)
                        if add_tags:
                            eh.add_op("set_tags", add_tags)
                # If there are no tags specified, make sure to remove any straggler tags
                else:
                    if current_tags:
                        eh.add_op("remove_tags", list(current_tags.keys()))

            # If the load balancer does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
            except client.exceptions.LoadBalancerNotFoundException:
                eh.add_log("Load Balancer Not Found", {"arn": load_balancer_arn})
                pass

        else:
            eh.add_log("Did not find load balancer")
        # else: # If there is no load balancer and there is no exception, handle it here
        #     eh.add_log("Load Balancer Does Not Exist", {"name": name})
        #     eh.add_op("create_load_balancer")
    # If there is no load balancer and there is an exception handle it here
    except client.exceptions.LoadBalancerNotFoundException:
        eh.add_log("Load Balancer Does Not Exist", {"name": name})
        eh.add_op("create_load_balancer")
        return 0
    except ClientError as e:
        print(str(e))
        eh.add_log("Get Load Balancer Error", {"error": str(e)}, is_error=True)
        eh.retry_error("Get Load Balancer Error", 10)
        return 0

            
@ext(handler=eh, op="create_load_balancer")
def create_load_balancer(attributes, special_attributes, default_special_attributes, region, prev_state):

    try:
        response = client.create_load_balancer(**attributes)
        load_balancer = response.get("LoadBalancers")[0]
        load_balancer_arn = load_balancer.get("LoadBalancerArn")

        eh.add_log("Created Load Balancer", load_balancer)
        eh.add_state({"load_balancer_arn": load_balancer.get("LoadBalancerArn"), "region": region, "name": load_balancer.get("LoadBalancerName")})
        eh.add_props({
            "name": load_balancer.get("LoadBalancerName"),
            "arn": load_balancer.get("LoadBalancerArn"),
            "dns_name": load_balancer.get("DNSName"),
            "canonical_hosted_zone_id": load_balancer.get("CanonicalHostedZoneId"),
            "vpc_id": load_balancer.get("VpcId"),
            "load_balancer_type": load_balancer.get("Type"),
            "security_groups": load_balancer.get("SecurityGroups"),
            "subnets": [item.get("SubnetId") for item in load_balancer.get("AvailabilityZones", [])],
            "scheme": load_balancer.get("Scheme"),
            "ip_address_type": load_balancer.get("IpAddressType")
        })
        eh.add_links({"Load Balancer": gen_load_balancer_link(region, load_balancer.get("LoadBalancerArn"))})
        eh.add_op("check_load_balancer_create_complete")

        ### Once the load_balancer exists, then setup any followup tasks

        # Set the load balancer ip address type and subnets
        if attributes.get("ip_address_type") != prev_state.get("props", {}).get("ip_address_type"):
            # Set the load balancer ip address type
            eh.add_op("set_ip_address_type")
        # Set the load balancer security groups (and ip address type, if network load balancer)
        if attributes.get("subnets") != prev_state.get("props", {}).get("subnets") or \
            (attributes.get("ip_address_type") != prev_state.get("props", {}).get("ip_address_type") and attributes["load_balancer_type"] == "network"):
            eh.add_op("set_subnets")
        # Set the load balancer security groups
        if attributes.get("security_groups") != prev_state.get("props", {}).get("security_groups"):
            eh.add_op("set_security_groups")

        # Figure out if there are special attributes that need to be set, otherwise reset all of the special attributes that exist current on the load balancer to their original values
        try:
            
            # Try to get the current special attributes
            response = client.describe_load_balancer_attributes(LoadBalancerArn=load_balancer_arn)
            current_special_attributes = {item.get("Key"): item.get("Value") for item in response["Attributes"]}
            if load_balancer.get("IpAddressType") == "ipv4":
                current_special_attributes.pop("ipv6.deny_all_igw_traffic", None)

            if special_attributes:
                # You either use whatever is being passed in or the default. That's it. And you only care about parameters that are allowed to be set for the current setup.
                update_special_attributes = remove_none_attributes({key: (special_attributes.get(key) or default_special_attributes.get(key)) for key, current_value in current_special_attributes.items() })
                eh.add_state({"update_special_attributes": update_special_attributes})
                eh.add_op("update_load_balancer_special_attributes", update_special_attributes)

            else:
                eh.add_state({"current_special_attributes": current_special_attributes})
                eh.add_op("reset_load_balancer_special_attributes", current_special_attributes)
        # If the load balancer does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
        except client.exceptions.LoadBalancerNotFoundException:
            eh.add_log("Load Balancer Not Found", {"arn": load_balancer_arn})
            pass

        try:
            # Try to get the current tags
            response = client.describe_tags(ResourceArns=[load_balancer_arn])
            eh.add_log("Got Tags")
            relevant_items = [item for item in response.get("TagDescriptions") if item.get("ResourceArn") == load_balancer_arn]
            current_tags = {}

            # Parse out the current tags
            if len(relevant_items) > 0:
                relevant_item = relevant_items[0]
                if relevant_item.get("Tags"):
                    current_tags = {item.get("Key") : item.get("Value") for item in relevant_item.get("Tags")}

            # If there are tags specified, figure out which ones need to be added and which ones need to be removed
            if attributes.get("Tags"):

                tags = attributes.get("Tags")
                formatted_tags = {item.get("Key") : item.get("Value") for item in tags}
                # Compare the current tags to the desired tags
                if formatted_tags != current_tags:
                    remove_tags = [k for k in current_tags.keys() if k not in formatted_tags]
                    add_tags = {k:v for k,v in formatted_tags.items() if v != current_tags.get(k)}
                    if remove_tags:
                        eh.add_op("remove_tags", remove_tags)
                    if add_tags:
                        eh.add_op("set_tags", add_tags)
            # If there are no tags specified, make sure to remove any straggler tags
            else:
                if current_tags:
                    eh.add_op("remove_tags", list(current_tags.keys()))

        # If the load balancer does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
        except client.exceptions.LoadBalancerNotFoundException:
            eh.add_log("Load Balancer Not Found", {"arn": load_balancer_arn})
            pass

    except client.exceptions.DuplicateLoadBalancerNameException as e:
        eh.add_log(f"Load Balancer name {attributes.get('Name')} already exists", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyLoadBalancersException as e:
        eh.add_log(f"AWS Quota for Load Balancers reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log("Invalid Load Balancer Parameters", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTagsException as e:
        eh.add_log("Too Many Tags on Load Balancer. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.SubnetNotFoundException as e:
        eh.add_log("Subnet provided was not found.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidSubnetException as e:
        eh.add_log("Invalid Subnet Provided", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidSecurityGroupException as e:
        eh.add_log("Invalid Security Group Provided", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidSchemeException as e:
        eh.add_log("Invalid Scheme Provided", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.DuplicateTagKeysException as e:
        eh.add_log("Tag already exists. Please check for duplicate tags.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    # except client.exceptions.ResourceInUseException as e: # Not sure when exactly this would happen on a create. Leaving it out. 
    #     eh.add_log("Resource is in Use.", {"error": str(e)}, is_error=True)
    #     eh.perm_error(str(e), 20)
    except client.exceptions.AvailabilityZoneNotSupportedException as e:
        eh.add_log("Availbility Zone not supported for Load Balancer. Please change your configuration to point to subnets within supported availbility zones.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.OperationNotPermittedException as e:
        eh.add_log("The operation you are taking is not permitted. Please change your configuration and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)


    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Load Balancer", progress=20)


@ext(handler=eh, op="check_load_balancer_create_complete")
def check_load_balancer_create_complete():

    name = eh.state["name"]
    try:
        response = client.describe_load_balancers(Names=[name])
        load_balancer_to_use = None
        load_balancer_status = None
        load_balancer_reason = None
        if response and response.get("LoadBalancers") and len(response.get("LoadBalancers")) > 0:
            load_balancer_to_use = response.get("LoadBalancers")[0]
            load_balancer_status = load_balancer_to_use.get("State", {}).get("Code")
            load_balancer_reason = load_balancer_to_use.get("State", {}).get("Reason")
            if load_balancer_status in ["active", "active_impaired"]:
                eh.add_log(f"Load Balancer Creation Succeeded {load_balancer_reason if load_balancer_reason else ''}", response)
            elif load_balancer_status == "failed":
                eh.add_log("Load Balancer Creation Failed", response, is_error=True)
                eh.perm_error(f"Load Balancer Creation Failed {load_balancer_reason if load_balancer_reason else ''}", progress=20)
            else: # it is in a "provisioning" state
                eh.add_log("Provisioning Load Balancer", response)
                eh.retry_error(current_epoch_time_usec_num(), progress=30, callback_sec=8)

        else:
            eh.add_log("Did not find load balancer")
        # else: # If there is no load balancer and there is no exception, handle it here
        #     eh.add_log("Load Balancer Does Not Exist", {"name": name})
        #     eh.add_op("create_load_balancer")
    # If there is no load balancer and there is an exception handle it here
    except client.exceptions.LoadBalancerNotFoundException:
        eh.add_log("Load Balancer Does Not Exist", {"name": name})
        return 0
    except ClientError as e:
        print(str(e))
        eh.add_log("Get Load Balancer Error", {"error": str(e)}, is_error=True)
        eh.retry_error("Get Load Balancer Error", 10)
        return 0

@ext(handler=eh, op="remove_tags")
def remove_tags():

    remove_tags = eh.ops.get('remove_tags')
    load_balancer_arn = eh.state["load_balancer_arn"]

    try:
        response = client.remove_tags(
            ResourceArns=[load_balancer_arn],
            TagKeys=remove_tags
        )
        eh.add_log("Removed Tags", remove_tags)
    except client.exceptions.LoadBalancerNotFoundException:
        eh.add_log("Load Balancer Not Found", {"arn": load_balancer_arn})

    except ClientError as e:
        handle_common_errors(e, eh, "Error Removing Load Balancer Tags", progress=90)


@ext(handler=eh, op="set_tags")
def set_tags():

    tags = eh.ops.get("set_tags")
    print(tags)
    load_balancer_arn = eh.state["load_balancer_arn"]
    try:
        response = client.add_tags(
            ResourceArns=[load_balancer_arn],
            Tags=[{"Key": key, "Value": value} for key, value in tags.items()]
        )
        eh.add_log("Tags Added", response)

    except client.exceptions.LoadBalancerNotFoundException as e:
        eh.add_log("Load Balancer Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)
    except client.exceptions.DuplicateTagKeysException as e:
        eh.add_log(f"Duplicate Tags Found. Please remove duplicates and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)
    except client.exceptions.TooManyTagsException as e:
        eh.add_log(f"Too Many Tags on Load Balancer. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Adding Tags", progress=90)

@ext(handler=eh, op="set_ip_address_type")
def set_ip_address_type(ip_address_type):

    load_balancer_arn = eh.state["load_balancer_arn"]
    region = eh.state["region"]

    try:
        response = client.set_ip_address_type(
            LoadBalancerArn=load_balancer_arn,
            IpAddressType=ip_address_type
        )

        eh.add_log("Modified Load Balancer IP Address Type", response)
        
        eh.add_props({
            "ip_address_type": response.get("IpAddressType")
        })

    except client.exceptions.LoadBalancerNotFoundException as e:
        eh.add_log("Load Balancer Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 70)
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log("Invalid Load Balancer IP Address Type", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 70)
    except client.exceptions.InvalidSubnetException as e:
        eh.add_log("Subnet incompatible with provided Load Balancer IP Address Type", {"error": str(e)}, is_error=True) # I believe that this error means this.
        eh.perm_error(str(e), 70)   

    except ClientError as e:
        handle_common_errors(e, eh, "Error Updating Load Balancer", progress=70)
    
@ext(handler=eh, op="set_security_groups")
def set_security_groups(security_groups):

    load_balancer_arn = eh.state["load_balancer_arn"]
    region = eh.state["region"]

    try:
        response = client.set_security_groups(
            LoadBalancerArn=load_balancer_arn,
            SecurityGroups=security_groups
        )

        eh.add_log("Modified Load Balancer Security Groups", response)
        
        eh.add_props({
            "security_groups": response.get("SecurityGroupIds")
        })

    except client.exceptions.LoadBalancerNotFoundException as e:
        eh.add_log("Load Balancer Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 70)
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log("Invalid Load Balancer Security Group Configuration provided", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 70)
    except client.exceptions.InvalidSecurityGroupException as e:
        eh.add_log("Invalid Security Group Provided", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 70)   

    except ClientError as e:
        handle_common_errors(e, eh, "Error Updating Load Balancer", progress=70)
    
@ext(handler=eh, op="set_subnets")
def set_subnets(subnets, ip_address_type, load_balancer_type):

    load_balancer_arn = eh.state["load_balancer_arn"]

    payload = {
        "LoadBalancerArn": load_balancer_arn,
        "Subnets": subnets,
    }
    if load_balancer_type == "network":
        payload["IpAddressType"] = ip_address_type
    try:
        response = client.set_subnets(**payload)

        eh.add_log("Modified Load Balancer Subnets", response)
        
        eh.add_props({
            "subnets": [item.get("SubnetId") for item in response.get("AvailabilityZones", [])],
            "ip_address_type": response.get("IpAddressType")
        })

    except client.exceptions.LoadBalancerNotFoundException as e:
        eh.add_log("Load Balancer Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 70)
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log("Invalid Load Balancer Subnets and IP Address Type combination provided", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 70)
    except client.exceptions.InvalidSubnetException as e:
        eh.add_log("Invalid Subnet Provided", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 70)   
    except client.exceptions.SubnetNotFoundException as e:
        eh.add_log("Subnet does not exist", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 70)  
    except client.exceptions.AvailabilityZoneNotSupportedException as e:
        eh.add_log("Availability Zone provided not supported. Please try a different availability zone.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 70)  

    except ClientError as e:
        handle_common_errors(e, eh, "Error Updating Load Balancer", progress=70)


@ext(handler=eh, op="update_load_balancer_special_attributes")
def update_load_balancer_special_attributes():

    load_balancer_arn = eh.state["load_balancer_arn"]
    update_special_attributes = eh.state["update_special_attributes"]
    formatted_update_attributes = [{"Key": key, "Value": value} for key, value in update_special_attributes.items()]

    try:
        response = client.modify_load_balancer_attributes(
            LoadBalancerArn=load_balancer_arn,
            Attributes=formatted_update_attributes
        )
        eh.add_log("Modified Load Balancer Special Attributes", response)
        # TODO: Add in progress update?
    # If the load balancer does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
    except client.exceptions.LoadBalancerNotFoundException:
        eh.add_log("Load Balancer Not Found", {"arn": load_balancer_arn})
    # Bad input config. Fail out
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log("Invalid Load Balancer Parameters", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Updating Load Balancer Special Attributes", progress=80)

@ext(handler=eh, op="reset_load_balancer_special_attributes")
def reset_load_balancer_special_attributes(default_special_attributes):

    load_balancer_arn = eh.state["load_balancer_arn"]
    current_special_attributes = eh.state["current_special_attributes"]

    update_attributes = remove_none_attributes({key: default_special_attributes.get(key) for key in current_special_attributes})
    formatted_update_attributes = [{"Key": key, "Value": value} for key, value in update_attributes.items()]
                         
    try:
        response = client.modify_load_balancer_attributes(
            LoadBalancerArn=load_balancer_arn,
            Attributes=formatted_update_attributes
        )
        eh.add_log("Modified Load Balancer Special Attributes", response)
        # TODO: Add in progress update?
    # If the load balancer does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
    except client.exceptions.LoadBalancerNotFoundException:
        eh.add_log("Load Balancer Not Found", {"arn": load_balancer_arn})
    # Bad input config. Fail out
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log("Invalid Load Balancer Parameters", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Updating Load Balancer Special Attributes", progress=80)
        

@ext(handler=eh, op="delete_load_balancer")
def delete_load_balancer():
    load_balancer_arn = eh.state["load_balancer_arn"]
    try:
        response = client.delete_load_balancer(
            LoadBalancerArn=load_balancer_arn
        )
        eh.add_log("Load Balancer Deleted", {"load_balancer_arn": load_balancer_arn})
    except client.exceptions.ResourceInUseException as e:
        handle_common_errors(e, eh, "Error Deleting Load Balancer. Resource in Use.", progress=80)
    except client.exceptions.OperationNotPermittedException: 
        eh.add_log("Delete Operation Not Permitted. Please check for dependencies.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except client.exceptions.LoadBalancerNotFoundException:
        eh.add_log("Old Load Balancer Doesn't Exist", {"load_balancer_arn": load_balancer_arn})
    except ClientError as e:
        handle_common_errors(e, eh, "Error Deleting Load Balancer", progress=80)
    



def gen_load_balancer_link(region, load_balancer_arn):
    return f"https://{region}.console.aws.amazon.com/ec2/home?region={region}#LoadBalancer:loadBalancerArn={load_balancer_arn}"


