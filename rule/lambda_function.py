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
    
def expand_list_dict_to_key_value_list_obj(list_obj, key_as_str=True, value_as_str=True):
    return [{
            "Key": f"{key}" if key_as_str else key, 
            "Value": f"{item.get(key)}" if value_as_str else item.get(key)
        } 
        for item in list_obj 
            for key in item
    ]

def expand_dict_to_key_value_list_obj(obj, key_as_str=True, value_as_str=True):
    return [{
            "Key": f"{key}" if key_as_str else key, 
            "Value": f"{value}" if value_as_str else value
        } 
        for key, value in obj.items()
    ]

def key_value_list_obj_to_compressed_list_dict(list_obj, key_as_str=True, value_as_str=True):
    return [{
        f'{item.get("Key")}' if key_as_str else item.get("Key"): 
        f'{item.get("Value")}' if value_as_str else item.get("Value")} 
        for item in list_obj
    ]

def key_value_list_obj_to_compressed_dict(list_obj, key_as_str=True, value_as_str=True):
    return {
        f'{item.get("Key")}' if key_as_str else item.get("Key"): 
        f'{item.get("Value")}' if value_as_str else item.get("Value")
        for item in list_obj
    }

def conditions_to_formatted_conditions(conditions):
    # Take conditions and convert them into the mass of config sections that the calls actually use
    formatted_conditions = []
    for item in conditions:
        if item.get("field") == "http-header":
            formatted_conditions.append({
                'Field': item.get("field"),
                'HttpHeaderConfig': {
                    'HttpHeaderName': item.get("http_header_name"),
                    'Values': item.get("values")
                }
            })
        elif item.get("field") == "http-request-method":
            formatted_conditions.append({
                'Field': item.get("field"),
                'HttpRequestMethodConfig': {
                    'Values': item.get("values")
                }
            })
        elif item.get("field") == "host-header":
            formatted_conditions.append({
                'Field': item.get("field"),
                'HostHeaderConfig': {
                    'Values': item.get("values")
                }
            })
        elif item.get("field") == "path-pattern":
            formatted_conditions.append({
                'Field': item.get("field"),
                'PathPatternConfig': {
                    'Values': item.get("values")
                }
            })
        elif item.get("field") == "query-string":
            formatted_conditions.append({
                'Field': item.get("field"),
                'QueryStringConfig': {
                    'Values': expand_list_dict_to_key_value_list_obj(item.get("values")) 
                    # [{"Key": f"{key}", "Value": f"{value}"} for key, value in item.get("values").items()]
                }
            })
        elif item.get("field") == "source-ip":
            formatted_conditions.append({
                'Field': item.get("field"),
                'SourceIpConfig': {
                    'Values': item.get("values")
                }
            })
        else:
            pass
    return formatted_conditions

def formatted_conditions_to_conditions(formatted_conditions):
    conditions = []
    for item in formatted_conditions:
        item_field = item.get("Field")
        if item_field == "http-header":
            conditions.append({
                'field': item_field,
                'values': item.get("HttpHeaderConfig", {}).get("Values"),
                'http_header_name': item.get("HttpHeaderConfig", {}).get("HttpHeaderName")
            })
        elif item_field == "http-request-method":
            conditions.append({
                'field': item_field,
                'values': item.get("HttpRequestMethodConfig", {}).get("Values"),
            })
        elif item_field == "host-header":
            conditions.append({
                'field': item_field,
                'values': item.get("HostHeaderConfig", {}).get("Values")
            })
        elif item_field == "path-pattern":
            conditions.append({
                'field': item_field,
                'values': item.get("PathPatternConfig", {}).get("Values")
            })
        elif item_field == "query-string":
            conditions.append({
                'field': item_field,
                'values': key_value_list_obj_to_compressed_list_dict(item.get("QueryStringConfig", {}).get("Values"))
            })
        elif item_field == "source-ip":
            conditions.append({
                'field': item_field,
                'values': item.get("SourceIpConfig", {}).get("Values")
            })
        else:
            pass
    return conditions


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
        listener_arn = cdef.get("listener_arn")
        target_group_arn = cdef.get("target_group_arn")
        conditions = cdef.get("conditions") # Format: [{"field": field, "values": [], "http_header_name": header_name (OPTIONAL)}]
        # Read the documentation to understand how to properly format the input conditions https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/elbv2/client/create_rule.html
        priority = cdef.get("priority")
        action_type = cdef.get("action_type") or "forward"
        # Removing advanced rule functionality until a customer needs it.
        tags = cdef.get('tags') # this is converted to a [{"Key": key, "Value": value} , ...] format

        formatted_conditions = conditions_to_formatted_conditions(conditions)

        # remove any None values from the attributes dictionary
        attributes = {}
        
        attributes = remove_none_attributes({
            "ListenerArn": str(listener_arn) if listener_arn else listener_arn,
            "Conditions": formatted_conditions if formatted_conditions else None,
            "Priority": int(priority) if priority else priority,
            "Actions": [{
                "Type": str(action_type) if action_type else action_type,
                "TargetGroupArn": str(target_group_arn) if target_group_arn else target_group_arn,
            }],
            "Tags": expand_dict_to_key_value_list_obj(tags) if tags else None
            # [{"Key": f"{key}", "Value": f"{value}"} for key, value in tags.items()] if tags else None
        })

        ### DECLARE STARTING POINT
        pass_back_data = event.get("pass_back_data", {}) # pass_back_data only exists if this is a RETRY
        # If a RETRY, then don't set starting point
        if pass_back_data:
            pass # If pass_back_data exists, then eh has already loaded in all relevant RETRY information.
        # If NOT retrying, and we are instead upserting, then we start with the GET STATE call
        elif event.get("op") == "upsert":

            old_listener_arn = None

            try:
                old_listener_arn = prev_state["props"]["old_listener_arn"]
            except:
                pass

            eh.add_op("get_rule")

            # If any non-editable fields have changed, we are choosing to fail. 
            # Therefore a switchover is necessary to change un-editable values.
            if ((old_listener_arn and old_listener_arn != old_listener_arn)):

                non_editable_error_message = "You may not edit the Listener ARN of an existing Listener Rule. Please create a new component to get the desired configuration."
                eh.add_log("Cannot edit non-editable field", {"error": non_editable_error_message}, is_error=True)
                eh.perm_error(non_editable_error_message, 10)

        # If NOT retrying, and we are instead deleting, then we start with the DELETE call 
        #   (sometimes you start with GET STATE if you need to make a call for the identifier)
        elif event.get("op") == "delete":
            eh.add_op("delete_rule")
            eh.add_state({"rule_arn": prev_state["props"]["arn"]})

        # The ordering of call declarations should generally be in the following order
        # GET STATE
        # CREATE
        # UPDATE
        # DELETE
        # GENERATE PROPS
        
        ### The section below DECLARES all of the calls that can be made. 
        ### The eh.add_op() function MUST be called for actual execution of any of the functions. 

        ### GET STATE
        get_rule(attributes, region, prev_state)

        ### DELETE CALL(S)
        delete_rule()

        ### CREATE CALL(S) (occasionally multiple)
        create_rule(attributes, region, prev_state)
        update_rule(attributes, region, prev_state)
        update_rule_priority(priority)
        
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
@ext(handler=eh, op="get_rule")
def get_rule(attributes, region, prev_state):
    client = boto3.client("elbv2")
    
    existing_rule_arn = prev_state.get("props", {}).get("arn")
    existing_listener_arn = prev_state.get("props", {}).get("listener_arn")

    if existing_rule_arn:
        # Try to get the rule. If you succeed, record the props and links from the current rule
        try:
            response = client.describe_rules(
                RuleArns=[existing_rule_arn]
                )
            rule_to_use = None
            if response and response.get("Rules") and len(response.get("Rules")) > 0:
                eh.add_log("Got Rule Attributes", response)
                rule_to_use = response.get("Rules")[0]
                rule_arn = rule_to_use.get("RuleArn")
                eh.add_state({"rule_arn": rule_to_use.get("RuleArn"), "region": region})

                reformatted_conditions = formatted_conditions_to_conditions(rule_to_use.get("Conditions"))

                existing_props = {
                    "arn": rule_to_use.get("RuleArn"),
                    "listener_arn": existing_listener_arn,
                    "priority": rule_to_use.get("Priority"),
                    "conditions": reformatted_conditions,
                    "action_type": rule_to_use.get("Actions", [{}])[0].get("Type"),
                    "target_group_arn": rule_to_use.get("Actions", [{}])[0].get("TargetGroupArn"),
                }
                eh.add_props(existing_props)
                eh.add_links({"Rule": gen_rule_link(region, rule_arn=rule_to_use.get("RuleArn"))})

                ### If the listener exists, then setup any followup tasks
                populated_existing_attributes = remove_none_attributes(existing_props)
                current_attributes = remove_none_attributes({
                    "ListenerArn": str(populated_existing_attributes.get("listener_arn")) if populated_existing_attributes.get("listener_arn") else populated_existing_attributes.get("listener_arn"),
                    "Priority": int(populated_existing_attributes.get("priority")) if populated_existing_attributes.get("priority") else str(populated_existing_attributes.get("priority")),
                    "Conditions": conditions_to_formatted_conditions(reformatted_conditions) if reformatted_conditions else None,
                    "Actions": [{
                        "Type": str(populated_existing_attributes.get("action_type")) if populated_existing_attributes.get("action_type") else populated_existing_attributes.get("action_type"),
                        "TargetGroupArn": str(populated_existing_attributes.get("target_group_arn")) if populated_existing_attributes.get("target_group_arn") else populated_existing_attributes.get("target_group_arn"),
                    }]
                })

                comparable_attributes = {i:attributes[i] for i in attributes if i not in ['Tags', 'Priority']}
                comparable_current_attributes = {i:current_attributes[i] for i in current_attributes if i not in ['Tags', 'Priority']}
                if comparable_attributes != comparable_current_attributes:
                    eh.add_op("update_rule")

                if attributes.get("Priority") != current_attributes.get("Priority"):
                    eh.add_op("update_rule_priority")

                try:
                    # Try to get the current tags
                    response = client.describe_tags(ResourceArns=[rule_arn])
                    eh.add_log("Got Tags")
                    relevant_items = [item for item in response.get("TagDescriptions") if item.get("ResourceArn") == rule_arn]
                    current_tags = {}

                    # Parse out the current tags
                    if len(relevant_items) > 0:
                        relevant_item = relevant_items[0]
                        if relevant_item.get("Tags"):
                            current_tags = key_value_list_obj_to_compressed_dict(relevant_item.get("Tags"))
                            # {item.get("Key") : item.get("Value") for item in relevant_item.get("Tags")}

                    # If there are tags specified, figure out which ones need to be added and which ones need to be removed
                    if attributes.get("Tags"):

                        tags = attributes.get("Tags")
                        formatted_tags = key_value_list_obj_to_compressed_dict(tags)
                        # {item.get("Key") : item.get("Value") for item in tags}
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
                except client.exceptions.RuleNotFoundException:
                    eh.add_log("Listener Rule Not Found", {"arn": rule_arn})
                    pass

            else:
                eh.add_log("Listener Rule Does Not Exist", {"listener_arn": existing_rule_arn})
                eh.add_op("create_rule")
                return 0
        # If there is no listener and there is an exception handle it here
        except client.exceptions.RuleNotFoundException:
            eh.add_log("Listener Rule Does Not Exist", {"rule_arn": existing_rule_arn})
            eh.add_op("create_rule")
            return 0
        except ClientError as e:
            print(str(e))
            eh.add_log("Get Listener Rule Error", {"error": str(e)}, is_error=True)
            eh.retry_error("Get Listener Rule Error", 10)
            return 0
    else:
        eh.add_log("Listener Rule Does Not Exist", {"rule_arn": existing_rule_arn})
        eh.add_op("create_rule")
        return 0

            
@ext(handler=eh, op="create_rule")
def create_rule(attributes, region, prev_state):

    try:
        response = client.create_rule(**attributes)
        rule = response.get("Rules")[0]
        rule_arn = rule.get("RuleArn")

        eh.add_log("Created Listener Rule", rule)
        eh.add_state({"rule_arn": rule.get("RuleArn"), "region": region})
        props_to_add = {
            "arn": rule.get("RuleArn"),
            "listener_arn": attributes.get("ListenerArn"),
            "conditions": formatted_conditions_to_conditions(rule.get("Conditions")),
            "priority": rule.get("Priority"),
            "action_type": rule.get("Actions", [{}])[0].get("Type"),
            "target_group_arn": rule.get("Actions", [{}])[0].get("TargetGroupArn"),
        }
        eh.add_props(props_to_add)
        eh.add_links({"Rule": gen_rule_link(region, rule_arn=rule.get("RuleArn"))})

        ### Once the listener rule exists, then setup any followup tasks

        try:
            # Try to get the current tags
            response = client.describe_tags(ResourceArns=[rule_arn])
            eh.add_log("Got Tags")
            relevant_items = [item for item in response.get("TagDescriptions") if item.get("ResourceArn") == rule_arn]
            current_tags = {}

            # Parse out the current tags
            if len(relevant_items) > 0:
                relevant_item = relevant_items[0]
                if relevant_item.get("Tags"):
                    current_tags = key_value_list_obj_to_compressed_dict(relevant_item.get("Tags"))
                    # {item.get("Key") : item.get("Value") for item in relevant_item.get("Tags")}

            # If there are tags specified, figure out which ones need to be added and which ones need to be removed
            if attributes.get("Tags"):

                tags = attributes.get("Tags")
                formatted_tags = key_value_list_obj_to_compressed_dict(tags)
                # {item.get("Key") : item.get("Value") for item in tags}
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
        except client.exceptions.RuleNotFoundException:
            eh.add_log("Rule Not Found", {"arn": rule_arn})
            pass

    except client.exceptions.PriorityInUseException as e:
        eh.add_log(f"Priority {attributes.get('Priority')} assigned to this listener rule already exists", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTargetGroupsException as e:
        eh.add_log(f"AWS Quota for Target Groups reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyRulesException as e:
        eh.add_log(f"AWS Quota for Rules per Listener reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TargetGroupAssociationLimitException as e:
        eh.add_log(f"Too many Target Groups associated with the Rule. Please decrease the number of Target Groups associated to this Listener Rule and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.ListenerNotFoundException as e:
        eh.add_log(f"Listener provided for this Listener Rule was not found.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TargetGroupNotFoundException as e:
        eh.add_log(f"Target Group provided for this Listener Rule was not found.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidConfigurationRequestException as e:
        eh.add_log(f"The configuration provided for this Listener Rule is invalid. Please enter valid configuration and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyRegistrationsForTargetIdException as e:
        eh.add_log(f"Too many registrations for the Target provided. Please decrease the number of registrations for the Target and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTargetsException as e:
        eh.add_log(f"Too many Targets provided for this Listener Rule. Please decrease the number of Targets for the Listener Rule and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyActionsException as e:
        eh.add_log(f"Too many Actions provided for this Listener Rule. Please decrease the number of Actions for the Listener Rule and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidLoadBalancerActionException as e:
        eh.add_log(f"Load Balancer action specified is invalid. Please change the Load Balancer action specified and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyUniqueTargetGroupsPerLoadBalancerException as e:
        eh.add_log(f"AWS Quota for Unique Target Groups per Load Balancer reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTagsException as e:
        eh.add_log("Too Many Tags on Listener Rule. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Listener Rule", progress=20)


@ext(handler=eh, op="remove_tags")
def remove_tags():

    remove_tags = eh.ops.get('remove_tags')
    rule_arn = eh.state["rule_arn"]

    try:
        response = client.remove_tags(
            ResourceArns=[rule_arn],
            TagKeys=remove_tags
        )
        eh.add_log("Removed Tags", remove_tags)
    except client.exceptions.ListenerNotFoundException:
        eh.add_log("Listener Rule Not Found", {"arn": rule_arn})

    except ClientError as e:
        handle_common_errors(e, eh, "Error Removing Listener Rule Tags", progress=90)


@ext(handler=eh, op="set_tags")
def set_tags():

    tags = eh.ops.get("set_tags")
    rule_arn = eh.state["rule_arn"]
    try:
        response = client.add_tags(
            ResourceArns=[rule_arn],
            Tags=[{"Key": key, "Value": value} for key, value in tags.items()]
        )
        eh.add_log("Tags Added", response)

    except client.exceptions.RuleNotFoundException as e:
        eh.add_log("Listener Rule Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)
    except client.exceptions.DuplicateTagKeysException as e:
        eh.add_log(f"Duplicate Tags Found. Please remove duplicates and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)
    except client.exceptions.TooManyTagsException as e:
        eh.add_log(f"Too Many Tags on Listener Rule. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 90)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Adding Tags", progress=90)


@ext(handler=eh, op="update_rule")
def update_rule(attributes, region, prev_state):
    modifiable_attributes = {i:attributes[i] for i in attributes if i not in ['Tags', "ListenerArn", "Priority"]}
    modifiable_attributes["RuleArn"] = eh.state["rule_arn"]
    try:
        response = client.modify_rule(**modifiable_attributes)
        rule = response.get("Rules")[0]
        rule_arn = rule.get("RuleArn")
        eh.add_log("Updated Listener Rule", rule)
        existing_props = {
            "arn": rule_arn,
            "listener_arn": attributes.get("ListenerArn"),
            "conditions": formatted_conditions_to_conditions(rule.get("Conditions")),
            "priority": rule.get("Priority"),
            "action_type": rule.get("Actions", [{}])[0].get("Type"),
            "target_group_arn": rule.get("Actions", [{}])[0].get("TargetGroupArn"),
        }
        eh.add_props(existing_props)
        eh.add_links({"Rule": gen_rule_link(region, rule_arn=rule_arn)})

    except client.exceptions.TargetGroupAssociationLimitException as e:
        eh.add_log(f"Too many Target Groups associated with the Rule. Please decrease the number of Target Groups associated to this Listener Rule and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.RuleNotFoundException as e:
        eh.add_log(f"Listener Rule Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.RuleNotFoundException as e:
        eh.add_log(f"The Operation specified is not permitted", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyRegistrationsForTargetIdException as e:
        eh.add_log(f"Too many registrations for the Target provided. Please decrease the number of registrations for the Target and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTargetsException as e:
        eh.add_log(f"Too many Targets provided for this Listener Rule. Please decrease the number of Targets for the Listener Rule and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TargetGroupNotFoundException as e:
        eh.add_log(f"Target Group provided for this Listener Rule was not found.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyActionsException as e:
        eh.add_log(f"Too many Actions provided for this Listener Rule. Please decrease the number of Actions for the Listener Rule and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidLoadBalancerActionException as e:
        eh.add_log(f"Load Balancer action specified is invalid. Please change the Load Balancer action specified and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyUniqueTargetGroupsPerLoadBalancerException as e:
        eh.add_log(f"AWS Quota for Unique Target Groups per Load Balancer reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Listener Rule", progress=20)

@ext(handler=eh, op="update_rule_priority")
def update_rule_priority(priority):

    rule_arn = eh.state["rule_arn"]
    try:
        response = client.set_rule_priorities(
            RulePriorities=[
                {
                    'RuleArn': rule_arn,
                    'Priority': int(priority) if priority else priority
                },
            ]
        )
        eh.add_props({"Priority": priority})
        eh.add_log(f"Updated Listener Rule Priority")
    except client.exceptions.RuleNotFoundException:
        eh.add_log("Listener Rule Not Found", {"rule_arn": rule_arn})
        eh.perm_error(str(e), 30)
    except client.exceptions.PriorityInUseException as e:
        eh.add_log(f"Priority {priority} assigned to this listener rule already exists", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 30)
    except client.exceptions.OperationNotPermittedException as e:
        eh.add_log(f"The operation of updating the priority on this listener rule is not permitted.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 30)
    except ClientError as e:
        handle_common_errors(e, eh, "Error Updating the Listener Rule Priority", progress=80)


@ext(handler=eh, op="delete_rule")
def delete_rule():
    rule_arn = eh.state["rule_arn"]
    try:
        response = client.delete_rule(
            RuleArn=rule_arn
        )
        eh.add_log("Listener Rule Deleted", {"rule_arn": rule_arn})
    except client.exceptions.RuleNotFoundException as e:
        eh.add_log(f"Listener Rule Not Found", {"error": str(e)}, is_error=True)
        return 0
    except ClientError as e:
        handle_common_errors(e, eh, "Error Deleting Listener Rule", progress=80)
    

def gen_rule_link(region, rule_arn):
    return f"https://{region}.console.aws.amazon.com/ec2/home?region={region}#ListenerRuleDetails:ruleArn={rule_arn}"


