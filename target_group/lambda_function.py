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

def run_command(command):
    command_list = command.split(' ')
    logger.info("Running shell command: \"{}\"".format(command))
    result = subprocess.run(command_list, stdout=subprocess.PIPE)
    logger.info("Command output:\n---\n{}\n---".format(result.stdout.decode('UTF-8')))
    return result

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
        vpc_id = cdef.get('vpc_id')
        protocol = cdef.get("protocol") or 'HTTPS'
        protocol_version = cdef.get('protocol_version') or "HTTP1"
        port = cdef.get('port') or 443
        health_check_protocol = cdef.get('health_check_protocol') or 'HTTPS'
        health_check_port = cdef.get('health_check_port') or 'traffic-port'
        health_check_enabled = cdef.get('health_check_enabled') or True
        health_check_path = cdef.get('health_check_path') or '/'
        health_check_interval_seconds = cdef.get('health_check_interval_seconds') or 30
        health_check_timeout_seconds = cdef.get('health_check_timeout_seconds') or 10
        healthy_threshold_count = cdef.get('healthy_threshold_count') or 5
        unhealthy_threshold_count = cdef.get('unhealthy_threshold_count') or 2
        matcher = cdef.get('matcher') or {"HttpCode": '200,403'}
        target_type = cdef.get('target_type') or 'ip'
        tags = cdef.get('tags') # this is converted to a [{"Key": key, "Value": value} , ...] format
        ip_address_type = cdef.get('ip_address_type') or 'ipv4'

        ### Targets for the target group
        targets = cdef.get('targets')

        ### SPECIAL ATTRIBUTES THAT CAN ONLY BE ADDED POST INITIAL CREATION
        # supported by all load balancers
        deregistration_delay_timeout_seconds = cdef.get('deregistration_delay_timeout_seconds')
        stickiness_enabled = cdef.get("stickiness_enabled")
        stickiness_type = cdef.get("stickiness_type")
        # supported by Application Load Balancers and Network Load Balancers
        load_balancing_cross_zone_enabled = cdef.get("load_balancing_cross_zone_enabled")
        target_group_health_dns_failover_minimum_healthy_targets_count = cdef.get("target_group_health_dns_failover_minimum_healthy_targets_count")
        target_group_health_dns_failover_minimum_healthy_targets_percentage = cdef.get("target_group_health_dns_failover_minimum_healthy_targets_percentage")
        target_group_health_unhealthy_state_routing_minimum_healthy_targets_count = cdef.get("target_group_health_unhealthy_state_routing_minimum_healthy_targets_count")
        target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage = cdef.get("target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage")
        # supported only if the load balancer is an Application Load Balancer and the target is an instance or an IP address
        load_balancing_algorithm_type = cdef.get("load_balancing_algorithm_type")
        slow_start_duration_seconds = cdef.get("slow_start_duration_seconds")
        stickiness_app_cookie_cookie_name = cdef.get("stickiness_app_cookie_cookie_name")
        stickiness_app_cookie_duration_seconds = cdef.get("stickiness_app_cookie_duration_seconds")
        stickiness_lb_cookie_duration_seconds = cdef.get("stickiness_lb_cookie_duration_seconds")
        # supported only if the load balancer is an Application Load Balancer and the target is a Lambda function
        lambda_multi_value_headers_enabled = cdef.get("lambda_multi_value_headers_enabled")
        # supported only by Network Load Balancers
        deregistration_delay_connection_termination_enabled = cdef.get("deregistration_delay_connection_termination_enabled")
        preserve_client_ip_enabled = cdef.get("preserve_client_ip_enabled")
        proxy_protocol_v2_enabled = cdef.get("proxy_protocol_v2_enabled")
        # supported only by Gateway Load Balancers
        target_failover_on_deregistration = cdef.get("target_failover_on_deregistration_on_unhealthy")
        target_failover_on_unhealthy = cdef.get("target_failover_on_deregistration_on_unhealthy")

        # prev_state_def = prev_state.get("def", {})

        # prev_deregistration_delay_timeout_seconds = prev_state_def.get('deregistration_delay_timeout_seconds')
        # prev_stickiness_enabled = prev_state_def.get("stickiness_enabled")
        # prev_stickiness_type = prev_state_def.get("stickiness_type")
        # # supported by Application Load Balancers and Network Load Balancers
        # prev_load_balancing_cross_zone_enabled = prev_state_def.get("load_balancing_cross_zone_enabled")
        # prev_target_group_health_dns_failover_minimum_healthy_targets_count = prev_state_def.get("target_group_health_dns_failover_minimum_healthy_targets_count")
        # prev_target_group_health_dns_failover_minimum_healthy_targets_percentage = prev_state_def.get("target_group_health_dns_failover_minimum_healthy_targets_percentage")
        # prev_target_group_health_unhealthy_state_routing_minimum_healthy_targets_count = prev_state_def.get("target_group_health_unhealthy_state_routing_minimum_healthy_targets_count")
        # prev_target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage = prev_state_def.get("target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage")
        # # supported only if the load balancer is an Application Load Balancer and the target is an instance or an IP address
        # prev_load_balancing_algorithm_type = prev_state_def.get("load_balancing_algorithm_type")
        # prev_slow_start_duration_seconds = prev_state_def.get("slow_start_duration_seconds")
        # prev_stickiness_app_cookie_cookie_name = prev_state_def.get("stickiness_app_cookie_cookie_name")
        # prev_stickiness_app_cookie_duration_seconds = prev_state_def.get("stickiness_app_cookie_duration_seconds")
        # prev_stickiness_lb_cookie_duration_seconds = prev_state_def.get("stickiness_lb_cookie_duration_seconds")
        # # supported only if the load balancer is an Application Load Balancer and the target is a Lambda function
        # prev_lambda_multi_value_headers_enabled = prev_state_def.get("lambda_multi_value_headers_enabled")
        # # supported only by Network Load Balancers
        # prev_deregistration_delay_connection_termination_enabled = prev_state_def.get("deregistration_delay_connection_termination_enabled")
        # prev_preserve_client_ip_enabled = prev_state_def.get("preserve_client_ip_enabled")
        # prev_proxy_protocol_v2_enabled = prev_state_def.get("proxy_protocol_v2_enabled")
        # # supported only by Gateway Load Balancers
        # prev_target_failover_on_deregistration = prev_state_def.get("target_failover_on_deregistration")
        # prev_target_failover_on_unhealthy = prev_state_def.get("target_failover_on_unhealthy")
        formatted_targets = None
        if targets:
            formatted_targets = [remove_none_attributes({
                'Id': item.get("id"),
                'Port': item.get("port"),
                'AvailabilityZone': item.get('availability_zone')
            }) for item in targets]
            
        # remove any None values from the attributes dictionary
        attributes = remove_none_attributes({
            "Name": name,
            "Protocol": protocol,
            "ProtocolVersion": protocol_version,
            "Port": port,
            "VpcId": vpc_id,
            "HealthCheckProtocol": health_check_protocol,
            "HealthCheckPort": health_check_port,
            "HealthCheckEnabled": health_check_enabled,
            "HealthCheckPath": health_check_path,
            "HealthCheckIntervalSeconds": health_check_interval_seconds,
            "HealthCheckTimeoutSeconds": health_check_timeout_seconds,
            "HealthyThresholdCount": healthy_threshold_count,
            "UnhealthyThresholdCount": unhealthy_threshold_count,
            "Matcher": matcher,
            "TargetType": target_type,
            "Tags": [{"Key": f"{key}", "Value": f"{value}"} for key, value in tags.items()] if tags else None,
            "IpAddressType": ip_address_type
        })

        special_attributes = remove_none_attributes({
            "deregistration_delay.timeout_seconds": str(deregistration_delay_timeout_seconds) if deregistration_delay_timeout_seconds else deregistration_delay_timeout_seconds,
            "stickiness.enabled": str(stickiness_enabled) if stickiness_enabled else stickiness_enabled,
            "stickiness.type": str(stickiness_type) if stickiness_type else stickiness_type,
            "load_balancing.cross_zone.enabled": str(load_balancing_cross_zone_enabled).lower() if load_balancing_cross_zone_enabled else load_balancing_cross_zone_enabled,
            "target_group_health.dns_failover.minimum_healthy_targets.count": str(target_group_health_dns_failover_minimum_healthy_targets_count) if target_group_health_dns_failover_minimum_healthy_targets_count else target_group_health_dns_failover_minimum_healthy_targets_count,
            "target_group_health.dns_failover.minimum_healthy_targets.percentage": str(target_group_health_dns_failover_minimum_healthy_targets_percentage) if target_group_health_dns_failover_minimum_healthy_targets_percentage else target_group_health_dns_failover_minimum_healthy_targets_percentage,
            "target_group_health.unhealthy_state_routing.minimum_healthy_targets.count": str(target_group_health_unhealthy_state_routing_minimum_healthy_targets_count) if target_group_health_unhealthy_state_routing_minimum_healthy_targets_count else target_group_health_unhealthy_state_routing_minimum_healthy_targets_count,
            "target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage": str(target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage) if target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage else target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage,
            "load_balancing.algorithm.type": str(load_balancing_algorithm_type) if load_balancing_algorithm_type else load_balancing_algorithm_type,
            "slow_start.duration_seconds": str(slow_start_duration_seconds) if slow_start_duration_seconds else slow_start_duration_seconds,
            "stickiness.app_cookie.cookie_name": str(stickiness_app_cookie_cookie_name) if stickiness_app_cookie_cookie_name else stickiness_app_cookie_cookie_name,
            "stickiness.app_cookie.duration_seconds": str(stickiness_app_cookie_duration_seconds) if stickiness_app_cookie_duration_seconds else stickiness_app_cookie_duration_seconds,
            "stickiness.lb_cookie.duration_seconds": str(stickiness_lb_cookie_duration_seconds) if stickiness_lb_cookie_duration_seconds else stickiness_lb_cookie_duration_seconds,
            "lambda.multi_value_headers.enabled": str(lambda_multi_value_headers_enabled).lower() if lambda_multi_value_headers_enabled else lambda_multi_value_headers_enabled,
            "deregistration_delay.connection_termination.enabled": str(deregistration_delay_connection_termination_enabled).lower() if deregistration_delay_connection_termination_enabled else deregistration_delay_connection_termination_enabled,
            "preserve_client_ip.enabled": str(preserve_client_ip_enabled).lower() if preserve_client_ip_enabled else preserve_client_ip_enabled,
            "proxy_protocol_v2.enabled": str(proxy_protocol_v2_enabled).lower() if proxy_protocol_v2_enabled else proxy_protocol_v2_enabled,
            "target_failover.on_deregistration": str(target_failover_on_deregistration) if target_failover_on_deregistration else target_failover_on_deregistration,
            "target_failover.on_unhealthy": str(target_failover_on_unhealthy) if target_failover_on_unhealthy else target_failover_on_unhealthy
        })



        # prev_state_special_attributes = remove_none_attributes({
        #     "deregistration_delay.timeout_seconds": prev_deregistration_delay_timeout_seconds,
        #     "stickiness.enabled": prev_stickiness_enabled,
        #     "stickiness.type": prev_stickiness_type,
        #     "load_balancing.cross_zone.enabled": prev_load_balancing_cross_zone_enabled,
        #     "target_group_health.dns_failover.minimum_healthy_targets.count": prev_target_group_health_dns_failover_minimum_healthy_targets_count,
        #     "target_group_health.dns_failover.minimum_healthy_targets.percentage": prev_target_group_health_dns_failover_minimum_healthy_targets_percentage,
        #     "target_group_health.unhealthy_state_routing.minimum_healthy_targets.count": prev_target_group_health_unhealthy_state_routing_minimum_healthy_targets_count,
        #     "target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage": prev_target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage,
        #     "load_balancing.algorithm.type": prev_load_balancing_algorithm_type,
        #     "slow_start.duration_seconds": prev_slow_start_duration_seconds,
        #     "stickiness.app_cookie.cookie_name": prev_stickiness_app_cookie_cookie_name,
        #     "stickiness.app_cookie.duration_seconds": prev_stickiness_app_cookie_duration_seconds,
        #     "stickiness.lb_cookie.duration_seconds": prev_stickiness_lb_cookie_duration_seconds,
        #     "lambda.multi_value_headers.enabled": prev_lambda_multi_value_headers_enabled,
        #     "deregistration_delay.connection_termination.enabled": prev_deregistration_delay_connection_termination_enabled,
        #     "preserve_client_ip.enabled": prev_preserve_client_ip_enabled,
        #     "proxy_protocol_v2.enabled": prev_proxy_protocol_v2_enabled,
        #     "target_failover.on_deregistration": prev_target_failover_on_deregistration,
        #     "target_failover.on_unhealthy": prev_target_failover_on_unhealthy
        # })

        default_special_attributes = remove_none_attributes({
            'target_group_health.unhealthy_state_routing.minimum_healthy_targets.count': '1', 
            'stickiness.enabled': 'false', 
            'target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage': 'off', 
            'deregistration_delay.timeout_seconds': '300', 
            'target_group_health.dns_failover.minimum_healthy_targets.count': 'off', 
            'stickiness.app_cookie.cookie_name': '', 
            'stickiness.type': 'lb_cookie', 
            'stickiness.lb_cookie.duration_seconds': '86400', 
            'slow_start.duration_seconds': '0', 
            'stickiness.app_cookie.duration_seconds': '86400', 
            'target_group_health.dns_failover.minimum_healthy_targets.percentage': 'off',
            'load_balancing.cross_zone.enabled': 'use_load_balancer_configuration', 
            'load_balancing.algorithm.type': 'round_robin',
            "lambda.multi_value_headers.enabled": 'false',
            "deregistration_delay.connection_termination.enabled": "false",
            "preserve_client_ip.enabled": 'true', # there are weird special cases for the default here that haven't been dealt with yet https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/elbv2/client/modify_target_group_attributes.html
            "proxy_protocol_v2.enabled": 'false',
            "target_failover.on_deregistration": 'no_rebalance',
            "target_failover.on_unhealthy": 'no_rebalance'

        })

        ### DECLARE STARTING POINT
        pass_back_data = event.get("pass_back_data", {}) # pass_back_data only exists if this is a RETRY
        # If a RETRY, then don't set starting point
        if pass_back_data:
            pass # If pass_back_data exists, then eh has already loaded in all relevant RETRY information.
        # If NOT retrying, and we are instead upserting, then we start with the GET STATE call
        elif event.get("op") == "upsert":

            old_name = None
            old_protocol = None
            old_protocol_version = None
            old_port = None
            old_vpc_id = None
            old_target_type = None
            old_ip_address_type = None

            try:
                old_name = prev_state["props"]["name"]
                old_protocol = prev_state["props"]["protocol"]
                old_protocol_version = prev_state["props"]["protocol_version"]
                old_port = prev_state["props"]["port"]
                old_vpc_id = prev_state["props"]["vpc_id"]
                old_target_type = prev_state["props"]["target_type"]
                old_ip_address_type = prev_state["props"]["ip_address_type"]
            except:
                pass

            eh.add_op("get_target_group")

            # If any non-editable fields have changed, we are choosing to fail. 
            # We are NOT choosing to delete and recreate because a listener may be attached and that MUST be removed before the target group can be deleted. 
            # Therefore a switchover is necessary to change un-editable values.
            if (old_name and old_name != name) or \
                (old_protocol and old_protocol != protocol) or \
                (old_protocol_version and old_protocol_version != protocol_version) or \
                (old_port and old_port != port) or \
                (old_vpc_id and old_vpc_id != vpc_id) or \
                (old_target_type and old_target_type != target_type) or \
                (old_ip_address_type and old_ip_address_type != ip_address_type):

                non_editable_error_message = "You may not edit the name, protocol, protocol_version, port, vpc_id, target_type, or ip_address_type on an existing target_group. Please create a new component and associate the listener to your updated target group to get the desired configuration."
                eh.add_log("Cannot edit non-editable field", {"error": non_editable_error_message}, is_error=True)
                eh.perm_error(non_editable_error_message, 10)

        # If NOT retrying, and we are instead deleting, then we start with the DELETE call 
        #   (sometimes you start with GET STATE if you need to make a call for the identifier)
        elif event.get("op") == "delete":
            eh.add_op("delete_target_group")
            eh.add_state({"target_group_arn": prev_state["props"]["arn"]})

        # The ordering of call declarations should generally be in the following order
        # GET STATE
        # CREATE
        # UPDATE
        # DELETE
        # GENERATE PROPS
        
        ### The section below DECLARES all of the calls that can be made. 
        ### The eh.add_op() function MUST be called for actual execution of any of the functions. 

        ### GET STATE
        get_target_group(name, attributes, formatted_targets, special_attributes, default_special_attributes, region, prev_state)

        ### DELETE CALL(S)
        delete_target_group()

        ### CREATE CALL(S) (occasionally multiple)
        create_target_group(attributes, formatted_targets, special_attributes, default_special_attributes, region, prev_state)
        
        ### UPDATE CALLS (common to have multiple)
        # You want ONE function per boto3 update call, so that retries come back to the EXACT same spot. 
        remove_tags()
        set_tags()
        register_targets()
        update_target_group(attributes)
        update_target_group_special_attributes()
        reset_target_group_special_attributes(default_special_attributes)

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
@ext(handler=eh, op="get_target_group")
def get_target_group(name, attributes, targets, special_attributes, default_special_attributes, region, prev_state):
    client = boto3.client("elbv2")

    if prev_state and prev_state.get("props") and prev_state.get("props").get("name"):
        prev_name = prev_state.get("props").get("name")
        if name != prev_name:
            eh.perm_error("Cannot Change Target Group Name", progress=0)
            return None
    
    # Try to get the target group. If you succeed, record the props and links from the current target group
    try:
        response = client.describe_target_groups(Names=[name])
        target_group_to_use = None
        target_group_arn = None
        if response and response.get("TargetGroups") and len(response.get("TargetGroups")) > 0:
            eh.add_log("Got Target Group Attributes", response)
            target_group_to_use = response.get("TargetGroups")[0]
            target_group_arn = target_group_to_use.get("TargetGroupArn")
            eh.add_state({"target_group_arn": target_group_to_use.get("TargetGroupArn"), "region": region})
            eh.add_props({
                "name": name,
                "arn": target_group_to_use.get("TargetGroupArn"),
                "vpc_id": target_group_to_use.get("VpcId"),
                "port": target_group_to_use.get("Port"),
                "load_balancer_arns": target_group_to_use.get("LoadBalancerArns"),
                "protocol": target_group_to_use.get("Protocol"),
                "protocol_version": target_group_to_use.get("ProtocolVersion"),
                "target_type": target_group_to_use.get("TargetType"),
                "ip_address_type": target_group_to_use.get("IpAddressType"),
                "targets": prev_state.get("props", {}).get("targets")
            })
            eh.add_links({"Target Group": gen_target_group_link(region, target_group_arn)})


            ### If the target_group exists, then setup any followup tasks

            # Figure out what targets needs to be removed and added and setup those actions
            prev_targets = prev_state.get("props", {}).get("targets")

            targets_comparable = []
            if targets:
                targets_comparable = [f"{item.get('Id')}${item.get('Port', '')}${item.get('AvailabilityZone', '')}" for item in targets]

            prev_targets_comparable= []
            if prev_targets:
                prev_targets_comparable = [f"{item.get('Id')}${item.get('Port', '')}${item.get('AvailabilityZone', '')}" for item in prev_targets]

            prev_targets_to_remove = [prev_target.split("$")[0] for prev_target in prev_targets_comparable if prev_target not in targets_comparable]
            targets_to_add = [remove_none_attributes({ \
                "Id": def_target.split("$")[0], \
                "Port": safe_cast(def_target.split("$")[1], int, def_target.split("$")[1]) or None, \
                "AvailabilityZone": def_target.split("$")[2] or None \
                }) for def_target in targets_comparable if def_target not in prev_targets_comparable]
            
            if targets_to_add:
                eh.add_op("register_targets", targets_to_add)
            if prev_targets_to_remove:
                eh.add_op("deregister_targets", prev_targets_to_remove)
            
            # Update the target group
            eh.add_op("update_target_group")
            
            # Figure out if there are special attributes that need to be set, otherwise reset all of the special attributes that exist current on the target group to their original values
            try:
                
                # Try to get the current special attributes
                response = client.describe_target_group_attributes(TargetGroupArn=target_group_arn)
                eh.add_log("Got Target Group Special Attributes", response)
                current_special_attributes = {item.get("Key"): item.get("Value") for item in response["Attributes"]}

                if special_attributes:
                    # You either use whatever is being passed in or the default. That's it. And you only care about parameters that are allowed to be set for the current setup.
                    update_attributes = {key: (special_attributes.get(key) or default_special_attributes.get(key)) for key, current_value in current_special_attributes.items() }
                    eh.add_state({"update_special_attributes": update_attributes})
                    eh.add_op("update_target_group_special_attributes")

                else:
                    eh.add_state({"current_special_attributes": current_special_attributes})
                    eh.add_op("reset_target_group_special_attributes")
            # If the target group does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
            except client.exceptions.TargetGroupNotFoundException:
                eh.add_log("Target Group Not Found", {"arn": target_group_arn})
                pass

            try:
                # Try to get the current tags
                response = client.describe_tags(ResourceArns=[target_group_arn])
                eh.add_log("Got Tags")
                relevant_items = [item for item in response.get("TagDescriptions") if item.get("ResourceArn") == target_group_arn]
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

            # If the target group does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
            except client.exceptions.TargetGroupNotFoundException:
                eh.add_log("Target Group Not Found", {"arn": target_group_arn})
                pass

        else:
            eh.add_log("Did not find target group")
        # else: # If there is no target group and there is no exception, handle it hereb
        #     eh.add_log("Target Group Does Not Exist", {"name": name})
        #     eh.add_op("create_target_group")
    # If there is no target group and there is an exception handle it here
    except client.exceptions.TargetGroupNotFoundException:
        eh.add_log("Target Group Does Not Exist", {"name": name})
        eh.add_op("create_target_group")
        return 0
    except ClientError as e:
        print(str(e))
        eh.add_log("Get Target Group Error", {"error": str(e)}, is_error=True)
        eh.retry_error("Get Target Group Error", 10)
        return 0

            
@ext(handler=eh, op="create_target_group")
def create_target_group(attributes, targets, special_attributes, default_special_attributes, region, prev_state):

    try:
        response = client.create_target_group(**attributes)
        target_group = response.get("TargetGroups")[0]
        target_group_arn = target_group.get("TargetGroupArn")

        eh.add_log("Created Target Group", target_group)
        eh.add_state({"target_group_arn": target_group.get("TargetGroupArn")})
        eh.add_props({
            "name": target_group.get("TargetGroupName"),
            "arn": target_group.get("TargetGroupArn"),
            "vpc_id": target_group.get("VpcId"),
            "port": target_group.get("Port"),
            "load_balancer_arns": target_group.get("LoadBalancerArns"),
            "protocol": target_group.get("Protocol"),
            "protocol_version": target_group.get("ProtocolVersion"),
            "target_type": target_group.get("TargetType"),
            "ip_address_type": target_group.get("IpAddressType")
        })

        eh.add_links({"Target Group": gen_target_group_link(region, target_group.get("TargetGroupArn"))})

        ### Once the target_group exists, then setup any followup tasks

        # Figure out what targets needs to be removed and added and setup those actions
        prev_targets = prev_state.get("props", {}).get("targets")

        targets_comparable = []
        if targets:
            targets_comparable = [f"{item.get('Id')}${item.get('Port', '')}${item.get('AvailabilityZone', '')}" for item in targets]

        prev_targets_comparable= []
        if prev_targets:
            prev_targets_comparable = [f"{item.get('Id')}${item.get('Port', '')}${item.get('AvailabilityZone', '')}" for item in prev_targets]

        prev_targets_to_remove = [prev_target.split("$")[0] for prev_target in prev_targets_comparable if prev_target not in targets_comparable]
        targets_to_add = [remove_none_attributes({ \
            "Id": def_target.split("$")[0], \
            "Port": safe_cast(def_target.split("$")[1], int, def_target.split("$")[1]) or None, \
            "AvailabilityZone": def_target.split("$")[2] or None \
            }) for def_target in targets_comparable if def_target not in prev_targets_comparable]
        
        if targets_to_add:
            eh.add_op("register_targets", targets_to_add)
        if prev_targets_to_remove:
            eh.add_op("deregister_targets", prev_targets_to_remove)

        # Figure out if there are special attributes that need to be set, otherwise reset all of the special attributes that exist current on the target group to their original values
        try:
            
            # Try to get the current special attributes
            response = client.describe_target_group_attributes(TargetGroupArn=target_group_arn)
            current_special_attributes = {item.get("Key"): item.get("Value") for item in response["Attributes"]}

            if special_attributes:
                # You either use whatever is being passed in or the default. That's it. And you only care about parameters that are allowed to be set for the current setup.
                update_special_attributes = {key: (special_attributes.get(key) or default_special_attributes.get(key)) for key, current_value in current_special_attributes.items() }
                eh.add_state({"update_special_attributes": update_special_attributes})
                eh.add_op("update_target_group_special_attributes", update_special_attributes)

            else:
                eh.add_state({"current_special_attributes": current_special_attributes})
                eh.add_op("reset_target_group_special_attributes", current_special_attributes)
        # If the target group does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
        except client.exceptions.TargetGroupNotFoundException:
            eh.add_log("Target Group Not Found", {"arn": target_group_arn})
            pass

        try:
            # Try to get the current tags
            response = client.describe_tags(ResourceArns=[target_group_arn])
            eh.add_log("Got Tags")
            relevant_items = [item for item in response.get("TagDescriptions") if item.get("ResourceArn") == target_group_arn]
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

        # If the target group does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
        except client.exceptions.TargetGroupNotFoundException:
            eh.add_log("Target Group Not Found", {"arn": target_group_arn})
            pass

    except client.exceptions.DuplicateTargetGroupNameException as e:
        eh.add_log(f"Target Group name {attributes.get('Name')} already exists", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTargetGroupsException as e:
        eh.add_log(f"AWS Quota for Target Groups reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log("Invalid Target Group Parameters", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTagsException as e:
        eh.add_log("Too Many Tags on Target Group. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Target Group", progress=20)

@ext(handler=eh, op="remove_tags")
def remove_tags():

    remove_tags = eh.ops.get('remove_tags')
    target_group_arn = eh.state["target_group_arn"]

    try:
        response = client.remove_tags(
            ResourceArns=[target_group_arn],
            TagKeys=remove_tags
        )
        eh.add_log("Removed Tags", remove_tags)
    except client.exceptions.TargetGroupNotFoundException:
        eh.add_log("Target Group Not Found", {"arn": target_group_arn})

    except ClientError as e:
        handle_common_errors(e, eh, "Error Removing Target Group Tags", progress=90)


@ext(handler=eh, op="set_tags")
def set_tags():

    tags = eh.ops.get("set_tags")
    print(tags)
    target_group_arn = eh.state["target_group_arn"]
    try:
        response = client.add_tags(
            ResourceArns=[target_group_arn],
            Tags=[{"Key": key, "Value": value} for key, value in tags.items()]
        )
        eh.add_log("Tags Added", response)

    except client.exceptions.TargetGroupNotFoundException as e:
        eh.add_log("Target Group Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)
    except client.exceptions.DuplicateTagKeysException as e:
        eh.add_log(f"Duplicate Tags Found. Please remove duplicates and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)
    except client.exceptions.TooManyTagsException as e:
        eh.add_log(f"Too Many Tags on Target Group. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Adding Tags", progress=90)

@ext(handler=eh, op="register_targets")
def register_targets():

    targets = eh.ops.get("register_targets")
    target_group_arn = eh.state["target_group_arn"]
    print(targets)
    print(target_group_arn)
    payload = {
        "TargetGroupArn": target_group_arn,
        "Targets":[ {
            "Id": "10.0.5.199",
            "Port": 443,
            "AvailabilityZone": "all"
        },]
    }
    print(payload)
    try:

        # command = f"aws elbv2 register-targets --cli-input-json \"{json.dumps(payload)}\""
        # print(command)
        # result = run_command(command)
        # print(result)

        # Register targets boto3 call returns InternalFailure on valid input. Going with CLI instead
        response = client.register_targets(**payload)
        #     TargetGroupArn=target_group_arn,
        #     Targets=targets
        # )
        eh.add_log("Targets Registered", response)
        eh.add_props({
            "targets": targets
        })

    except client.exceptions.TargetGroupNotFoundException as e:
        eh.add_log("Target Group Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 60)
    except client.exceptions.InvalidTargetException as e:
        eh.add_log(f"Target provided is invalid. Please correct and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 60)
    except client.exceptions.TooManyTargetsException as e:
        eh.add_log(f"Too Many Targets on Target Group.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 60)
    except client.exceptions.TooManyRegistrationsForTargetIdException as e:
        eh.add_log(f"Too many registrations for a target id. Decrease the number of registrations for the target id and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 60)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Registering Targets", progress=60)

@ext(handler=eh, op="deregister_targets")
def deregister_targets():

    targets = eh.ops.get("deregister_targets")
    formatted_targets = [{"Id": item} for item in targets]
    target_group_arn = eh.state["target_group_arn"]
    try:
        response = client.deregister_targets(
            TargetGroupArn=target_group_arn,
            Targets=formatted_targets
        )
        eh.add_log("Targets Deregistered", response)
    except client.exceptions.TargetGroupNotFoundException as e:
        eh.add_log("Target Group Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 60)
    except client.exceptions.InvalidTargetException as e:
        eh.add_log(f"Target provided is invalid. Please correct and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 60)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Deregistering Targets", progress=60)

@ext(handler=eh, op="update_target_group")
def update_target_group(attributes):
    attributes_to_remove = ["Name", "Protocol", "ProtocolVersion", "Port", "VpcId", "TargetType", "Tags", "IpAddressType"]
    filtered_attributes = {attr: attributes[attr] for attr in attributes if attr not in attributes_to_remove}
    filtered_attributes["TargetGroupArn"] = eh.state["target_group_arn"]
    
    region = eh.state["region"]

    try:
        response = client.modify_target_group(**filtered_attributes)

        eh.add_log("Modified Target Group", response)

        if response and response.get("TargetGroups") and len(response.get("TargetGroups")) > 0:
            relevant_target_group = response.get("TargetGroups")[0]
            eh.add_state({"target_group_arn": relevant_target_group.get("TargetGroupArn")})
            eh.add_props({
                "name": relevant_target_group.get("TargetGroupName"),
                "arn": relevant_target_group.get("TargetGroupArn"),
                "vpc_id": relevant_target_group.get("VpcId"),
                "port": relevant_target_group.get("Port"),
                "load_balancer_arns": relevant_target_group.get("LoadBalancerArns"),
                "protocol": relevant_target_group.get("Protocol"),
                "protocol_version": relevant_target_group.get("ProtocolVersion"),
                "target_type": relevant_target_group.get("TargetType"),
                "ip_address_type": relevant_target_group.get("IpAddressType")
            })
            eh.add_links({"Target Group": gen_target_group_link(region, relevant_target_group.get("TargetGroupArn"))})
            
    except client.exceptions.TargetGroupNotFoundException as e:
        eh.add_log("Target Group Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 70)
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log("Invalid Target Group Parameters", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 70)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Updating Target Group", progress=70)
    

@ext(handler=eh, op="update_target_group_special_attributes")
def update_target_group_special_attributes():

    target_group_arn = eh.state["target_group_arn"]
    # update_special_attributes = eh.ops.get("update_target_group_special_attributes") # Pre-calculated in the get call get pull it in here and make the change
    update_special_attributes = eh.state["update_special_attributes"]
    formatted_update_attributes = [{"Key": key, "Value": value} for key, value in update_special_attributes.items()]

    try:
        response = client.modify_target_group_attributes(
            TargetGroupArn=target_group_arn,
            Attributes=formatted_update_attributes
        )
        eh.add_log("Modified Target Group Special Attributes", response)
        # TODO: Add in progress update?
    # If the target group does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
    except client.exceptions.TargetGroupNotFoundException:
        eh.add_log("Target Group Not Found", {"arn": target_group_arn})
    # Bad input config. Fail out
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log("Invalid Target Group Parameters", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Updating Target Group Special Attributes", progress=80)

@ext(handler=eh, op="reset_target_group_special_attributes")
def reset_target_group_special_attributes(default_special_attributes):

    target_group_arn = eh.state["target_group_arn"]
    # current_special_attributes = eh.state["current_special_attributes"] # Pre-calculated in the get call get pull it in here and make the change
    current_special_attributes = eh.state["current_special_attributes"]

    update_attributes = {key: default_special_attributes.get(key) for key in current_special_attributes}
    formatted_update_attributes = [{"Key": key, "Value": value} for key, value in update_attributes.items()]
                         
    try:
        response = client.modify_target_group_attributes(
            TargetGroupArn=target_group_arn,
            Attributes=formatted_update_attributes
        )
        eh.add_log("Modified Target Group Special Attributes", response)
        # TODO: Add in progress update?
    # If the target group does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
    except client.exceptions.TargetGroupNotFoundException:
        eh.add_log("Target Group Not Found", {"arn": target_group_arn})
    # Bad input config. Fail out
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log("Invalid Target Group Parameters", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Updating Target Group Special Attributes", progress=80)
        


@ext(handler=eh, op="delete_target_group")
def delete_target_group():
    target_group_arn = eh.state["target_group_arn"]
    try:
        response = client.delete_target_group(
            TargetGroupArn=target_group_arn
        )
        eh.add_log("Target Group Deleted", {"target_group_arn": target_group_arn})
    except client.exceptions.ResourceInUseException as e:
        handle_common_errors(e, eh, "Error Deleting Target Group. Resource in Use.", progress=80)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Deleting Target Group", progress=80)
    



def gen_target_group_link(region, target_group_arn):
    return f"https://{region}.console.aws.amazon.com/ec2/home?region={region}#TargetGroup:targetGroupArn={target_group_arn}"


