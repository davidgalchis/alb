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

        # you pull in whatever arguments you care about
        """
        # Some examples. S3 doesn't really need any because each attribute is a separate call. 
        auto_verified_attributes = cdef.get("auto_verified_attributes") or ["email"]
        alias_attributes = cdef.get("alias_attributes") or ["preferred_username", "phone_number", "email"]
        username_attributes = cdef.get("username_attributes") or None
        """

        ### ATTRIBUTES THAT CAN BE SET ON INITIAL CREATION
        load_balancer_arn = cdef.get("load_balancer_arn")
        protocol = cdef.get("protocol") or "HTTPS"
        port = cdef.get("port") or 443
        ssl_policy = cdef.get("ssl_policy") or "ELBSecurityPolicy-TLS13-1-2-2021-06" # To see full list, go here: https://docs.aws.amazon.com/elasticloadbalancing/latest/application/create-https-listener.html#describe-ssl-policies
        certificate_arn = cdef.get("certificate_arn")
        # Removing advanced listener functionality until a customer needs it. Needlessly complex, and most of it can't be set in the UI anyways, so it can't be a very common use case.
        action_type = cdef.get("action_type") or "forward"
        target_group_arn = cdef.get("target_group_arn")
        tags = cdef.get('tags') # this is converted to a [{"Key": key, "Value": value} , ...] format


        # remove any None values from the attributes dictionary
        attributes = remove_none_attributes({
            "LoadBalancerArn": str(load_balancer_arn) if load_balancer_arn else load_balancer_arn,
            "Protocol": str(protocol) if protocol else protocol,
            "Port": int(port) if port else port,
            "SslPolicy": str(ssl_policy) if ssl_policy else ssl_policy,
            "Certificates": [{"CertificateArn": certificate_arn}] if certificate_arn else None,
            "DefaultActions": [{
                "Type": str(action_type) if action_type else action_type,
                "TargetGroupArn": str(target_group_arn) if target_group_arn else target_group_arn,
            }],
            "Tags": [{"Key": f"{key}", "Value": f"{value}"} for key, value in tags.items()] if tags else None
        })

        ### DECLARE STARTING POINT
        pass_back_data = event.get("pass_back_data", {}) # pass_back_data only exists if this is a RETRY
        # If a RETRY, then don't set starting point
        if pass_back_data:
            pass # If pass_back_data exists, then eh has already loaded in all relevant RETRY information.
        # If NOT retrying, and we are instead upserting, then we start with the GET STATE call
        elif event.get("op") == "upsert":

            old_load_balancer_arn = None
            old_port = None

            try:
                old_load_balancer_arn = prev_state["props"]["load_balancer_arn"]
                old_port = prev_state["props"]["port"]
            except:
                pass

            eh.add_op("get_listener")

            # If any non-editable fields have changed, we are choosing to fail. 
            # Therefore a switchover is necessary to change un-editable values.
            if ((old_load_balancer_arn and old_load_balancer_arn != load_balancer_arn) \
                or (old_port and old_port != port)):

                non_editable_error_message = "You may not edit the Load Balancer ARN or the port of an existing listener. Please create a new component to get the desired configuration."
                eh.add_log("Cannot edit non-editable field", {"error": non_editable_error_message}, is_error=True)
                eh.perm_error(non_editable_error_message, 10)

        # If NOT retrying, and we are instead deleting, then we start with the DELETE call 
        #   (sometimes you start with GET STATE if you need to make a call for the identifier)
        elif event.get("op") == "delete":
            eh.add_op("delete_listener")
            eh.add_state({"listener_arn": prev_state["props"]["arn"]})

        # The ordering of call declarations should generally be in the following order
        # GET STATE
        # CREATE
        # UPDATE
        # DELETE
        # GENERATE PROPS
        
        ### The section below DECLARES all of the calls that can be made. 
        ### The eh.add_op() function MUST be called for actual execution of any of the functions. 

        ### GET STATE
        get_listener(attributes, region, prev_state)

        ### DELETE CALL(S)
        delete_listener()

        ### CREATE CALL(S) (occasionally multiple)
        create_listener(attributes, region, prev_state)
        update_listener(attributes, region, prev_state)
        
        ### UPDATE CALLS (common to have multiple)
        # You want ONE function per boto3 update call, so that retries come back to the EXACT same spot. 
        remove_tags()
        set_tags()

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
@ext(handler=eh, op="get_listener")
def get_listener(attributes, region, prev_state):
    client = boto3.client("elbv2")
    
    existing_listener_arn = prev_state.get("props", {}).get("arn")

    if existing_listener_arn:
        # Try to get the listener. If you succeed, record the props and links from the current listener
        try:
            response = client.describe_listeners(
                ListenerArns=[existing_listener_arn]
                )
            listener_to_use = None
            if response and response.get("Listeners") and len(response.get("Listeners")) > 0:
                eh.add_log("Got Listener Attributes", response)
                listener_to_use = response.get("Listeners")[0]
                listener_arn = listener_to_use.get("ListenerArn")
                eh.add_state({"listener_arn": listener_to_use.get("ListenerArn"), "region": region})
                existing_props = {
                    "arn": listener_to_use.get("ListenerArn"),
                    "load_balancer_arn": listener_to_use.get("LoadBalancerArn"),
                    "port": listener_to_use.get("Port"),
                    "protocol": listener_to_use.get("Protocol"),
                    "action_type": listener_to_use.get("DefaultActions", [{}])[0].get("Type"),
                    "target_group_arn": listener_to_use.get("DefaultActions", [{}])[0].get("TargetGroupArn"),
                    "ssl_policy": listener_to_use.get("SslPolicy"),
                    "certificate_arn": listener_to_use.get("Certificates", [{}])[0].get("CertificateArn")
                }
                eh.add_props(existing_props)
                eh.add_links({"Listener": gen_listener_link(region, load_balancer_arn=listener_to_use.get("LoadBalancerArn"), port=listener_to_use.get("Port"))})

                ### If the listener exists, then setup any followup tasks
                populated_existing_attributes = remove_none_attributes(existing_props)
                current_attributes = remove_none_attributes({
                    "LoadBalancerArn": str(populated_existing_attributes.get("load_balancer_arn")) if populated_existing_attributes.get("load_balancer_arn") else populated_existing_attributes.get("load_balancer_arn"),
                    "Protocol": str(populated_existing_attributes.get("protocol")) if populated_existing_attributes.get("protocol") else str(populated_existing_attributes.get("protocol")),
                    "Port": int(populated_existing_attributes.get("port")) if populated_existing_attributes.get("port") else populated_existing_attributes.get("port"),
                    "SslPolicy": str(populated_existing_attributes.get("ssl_policy")) if populated_existing_attributes.get("ssl_policy") else str(populated_existing_attributes.get("ssl_policy")),
                    "Certificates": [{"CertificateArn": populated_existing_attributes.get("certificate_arn") }] if populated_existing_attributes.get("certificate_arn") else None,
                    "DefaultActions": [{
                        "Type": str(populated_existing_attributes.get("action_type")) if populated_existing_attributes.get("action_type") else populated_existing_attributes.get("action_type"),
                        "TargetGroupArn": str(populated_existing_attributes.get("target_group_arn")) if populated_existing_attributes.get("target_group_arn") else populated_existing_attributes.get("target_group_arn"),
                    }]
                })

                comparable_attributes = {i:attributes[i] for i in attributes if i!='Tags'}
                if comparable_attributes != current_attributes:
                    eh.add_op("update_listener")

                try:
                    # Try to get the current tags
                    response = client.describe_tags(ResourceArns=[listener_arn])
                    eh.add_log("Got Tags")
                    relevant_items = [item for item in response.get("TagDescriptions") if item.get("ResourceArn") == listener_arn]
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
                except client.exceptions.ListenerNotFoundException:
                    eh.add_log("Listener Not Found", {"arn": listener_arn})
                    pass

            else:
                eh.add_log("Listener Does Not Exist", {"listener_arn": existing_listener_arn})
                eh.add_op("create_listener")
                return 0
        # If there is no listener and there is an exception handle it here
        except client.exceptions.ListenerNotFoundException:
            eh.add_log("Listener Does Not Exist", {"listener_arn": existing_listener_arn})
            eh.add_op("create_listener")
            return 0
        except ClientError as e:
            print(str(e))
            eh.add_log("Get Listener Error", {"error": str(e)}, is_error=True)
            eh.retry_error("Get Listener Error", 10)
            return 0
    else:
        eh.add_log("Listener Does Not Exist", {"listener_arn": existing_listener_arn})
        eh.add_op("create_listener")
        return 0

            
@ext(handler=eh, op="create_listener")
def create_listener(attributes, region, prev_state):

    try:
        response = client.create_listener(**attributes)
        listener = response.get("Listeners")[0]
        listener_arn = listener.get("ListenerArn")

        eh.add_log("Created Listener", listener)
        eh.add_state({"listener_arn": listener.get("ListenerArn"), "region": region})
        eh.add_props({
            "arn": listener.get("ListenerArn"),
            "load_balancer_arn": listener.get("LoadBalancerArn"),
            "port": listener.get("Port"),
            "protocol": listener.get("Protocol"),
            "action_type": listener.get("DefaultActions", [{}])[0].get("Type"),
            "target_group_arn": listener.get("DefaultActions", [{}])[0].get("TargetGroupArn"),
            "ssl_policy": listener.get("SslPolicy"),
            "certificate_arn": listener.get("Certificates", [{}])[0].get("CertificateArn")
        })
        eh.add_links({"Listener": gen_listener_link(region, load_balancer_arn=listener.get("LoadBalancerArn"), port=listener.get("Port"))})

        ### Once the listener exists, then setup any followup tasks

        try:
            # Try to get the current tags
            response = client.describe_tags(ResourceArns=[listener_arn])
            eh.add_log("Got Tags")
            relevant_items = [item for item in response.get("TagDescriptions") if item.get("ResourceArn") == listener_arn]
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
        except client.exceptions.ListenerNotFoundException:
            eh.add_log("Listener Not Found", {"arn": listener_arn})
            pass

    except client.exceptions.DuplicateListenerException as e:
        eh.add_log(f"Listener already exists", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyListenersException as e:
        eh.add_log(f"AWS Quota for Listeners reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyCertificatesException as e:
        eh.add_log(f"AWS Quota for Certificates per Listener reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.LoadBalancerNotFoundException as e:
        eh.add_log("Load Balancer provided was not found.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TargetGroupNotFoundException as e:
        eh.add_log("Target Group provided was not found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TargetGroupAssociationLimitException as e:
        eh.add_log("Limit for maximum target groups associated with a listener has been reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log("Invalid Listener Parameters", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.IncompatibleProtocolsException as e:
        eh.add_log("Incompatible Listener Protocol provided. The target group and listener protocols must be compatible.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.SSLPolicyNotFoundException as e:
        eh.add_log("SSL Policy provided was not found. Please try again with a different SSL Policy.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.CertificateNotFoundException as e:
        eh.add_log("Certificate provided was not found. Please try again with a different certificate.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.UnsupportedProtocolException as e:
        eh.add_log("Protocol provided is not supported. Please try again with a different protocol.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyRegistrationsForTargetIdException as e:
        eh.add_log("You have hit the limit for registrations for a target group. Please create a different target group to target.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTargetsException as e:
        eh.add_log("Too many Target Groups have been provided. Please provide fewer target groups.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyActionsException as e:
        eh.add_log("Too many Actions have been specified. Please specify fewer actions", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidLoadBalancerActionException as e:
        eh.add_log("The load balancer action specified is invalid. Please specify a valid load balancer action.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyUniqueTargetGroupsPerLoadBalancerException as e:
        eh.add_log("AWS Quota for Target Groups per Load Balancer reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTagsException as e:
        eh.add_log("Too Many Tags on Listener. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Listener", progress=20)


@ext(handler=eh, op="remove_tags")
def remove_tags():

    remove_tags = eh.ops.get('remove_tags')
    listener_arn = eh.state["listener_arn"]

    try:
        response = client.remove_tags(
            ResourceArns=[listener_arn],
            TagKeys=remove_tags
        )
        eh.add_log("Removed Tags", remove_tags)
    except client.exceptions.ListenerNotFoundException:
        eh.add_log("Listener Not Found", {"arn": listener_arn})

    except ClientError as e:
        handle_common_errors(e, eh, "Error Removing Listener Tags", progress=90)


@ext(handler=eh, op="set_tags")
def set_tags():

    tags = eh.ops.get("set_tags")
    listener_arn = eh.state["listener_arn"]
    try:
        response = client.add_tags(
            ResourceArns=[listener_arn],
            Tags=[{"Key": key, "Value": value} for key, value in tags.items()]
        )
        eh.add_log("Tags Added", response)

    except client.exceptions.ListenerNotFoundException as e:
        eh.add_log("Listener Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)
    except client.exceptions.DuplicateTagKeysException as e:
        eh.add_log(f"Duplicate Tags Found. Please remove duplicates and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)
    except client.exceptions.TooManyTagsException as e:
        eh.add_log(f"Too Many Tags on Listener. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Adding Tags", progress=90)


@ext(handler=eh, op="update_listener")
def update_listener(attributes, region, prev_state):
    modifiable_attributes = {i:attributes[i] for i in attributes if i not in ['Tags', "LoadBalancerArn"]}
    modifiable_attributes["ListenerArn"] = eh.state["listener_arn"]
    try:
        response = client.modify_listener(**modifiable_attributes)
        listener = response.get("Listeners")[0]
        listener_arn = listener.get("ListenerArn")
        eh.add_log("Updated Listener", listener)
        existing_props = {
            "arn": listener.get("ListenerArn"),
            "load_balancer_arn": listener.get("LoadBalancerArn"),
            "port": listener.get("Port"),
            "protocol": listener.get("Protocol"),
            "action_type": listener.get("DefaultActions", [{}])[0].get("Type"),
            "target_group_arn": listener.get("DefaultActions", [{}])[0].get("TargetGroupArn"),
            "ssl_policy": listener.get("SslPolicy"),
            "certificate_arn": listener.get("Certificates", [{}])[0].get("CertificateArn")
        }
        eh.add_props(existing_props)
        eh.add_links({"Listener": gen_listener_link(region, load_balancer_arn=listener.get("LoadBalancerArn"), port=listener.get("Port"))})

    except client.exceptions.ListenerNotFoundException as e:
        eh.add_log("Listener Does Not Exist", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.DuplicateListenerException as e:
        eh.add_log(f"Listener already exists", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyListenersException as e:
        eh.add_log(f"AWS Quota for Listeners reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyCertificatesException as e:
        eh.add_log(f"AWS Quota for Certificates per Listener reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.LoadBalancerNotFoundException as e:
        eh.add_log("Load Balancer provided was not found.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TargetGroupNotFoundException as e:
        eh.add_log("Target Group provided was not found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TargetGroupAssociationLimitException as e:
        eh.add_log("Limit for maximum target groups associated with a listener has been reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log("Invalid Listener Parameters", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.IncompatibleProtocolsException as e:
        eh.add_log("Incompatible Listener Protocol provided. The target group and listener protocols must be compatible.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.SSLPolicyNotFoundException as e:
        eh.add_log("SSL Policy provided was not found. Please try again with a different SSL Policy.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.CertificateNotFoundException as e:
        eh.add_log("Certificate provided was not found. Please try again with a different certificate.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.UnsupportedProtocolException as e:
        eh.add_log("Protocol provided is not supported. Please try again with a different protocol.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyRegistrationsForTargetIdException as e:
        eh.add_log("You have hit the limit for registrations for a target group. Please create a different target group to target.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTargetsException as e:
        eh.add_log("Too many Target Groups have been provided. Please provide fewer target groups.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyActionsException as e:
        eh.add_log("Too many Actions have been specified. Please specify fewer actions", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidLoadBalancerActionException as e:
        eh.add_log("The load balancer action specified is invalid. Please specify a valid load balancer action.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyUniqueTargetGroupsPerLoadBalancerException as e:
        eh.add_log("AWS Quota for Target Groups per Load Balancer reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTagsException as e:
        eh.add_log("Too Many Tags on Listener. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Listener", progress=20)



@ext(handler=eh, op="delete_listener")
def delete_listener():
    listener_arn = eh.state["listener_arn"]
    try:
        response = client.delete_listener(
            ListenerArn=listener_arn
        )
        eh.add_log("Listener Deleted", {"listener_arn": listener_arn})
    except client.exceptions.ResourceInUseException as e:
        handle_common_errors(e, eh, "Error Deleting Listener. Resource in Use.", progress=80)
    except client.exceptions.ListenerNotFoundException:
        eh.add_log("Old Listener Doesn't Exist", {"listener_arn": listener_arn})
        return 0
    except ClientError as e:
        handle_common_errors(e, eh, "Error Deleting Listener", progress=80)
    



def gen_listener_link(region, load_balancer_arn, port):
    return f"https://{region}.console.aws.amazon.com/ec2/home?region={region}#ELBListenerV2:loadBalancerArn={load_balancer_arn};listenerPort={port}"


