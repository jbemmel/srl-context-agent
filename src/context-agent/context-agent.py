#!/usr/bin/env python
# coding=utf-8

import grpc
from datetime import datetime, timezone
import sys
import logging
import socket
import os
import re
import ipaddress
import json
import signal
import traceback
import subprocess
from threading import Timer

import sdk_service_pb2
import sdk_service_pb2_grpc
import config_service_pb2

# To report state back
import telemetry_service_pb2
import telemetry_service_pb2_grpc

from pygnmi.client import gNMIclient, telemetryParser

from logging.handlers import RotatingFileHandler

############################################################
## Agent will start with this name
############################################################
agent_name='context_agent'

############################################################
## Open a GRPC channel to connect to sdk_mgr on the dut
## sdk_mgr will be listening on 50053
############################################################
#channel = grpc.insecure_channel('unix:///opt/srlinux/var/run/sr_sdk_service_manager:50053')
channel = grpc.insecure_channel('127.0.0.1:50053')
metadata = [('agent_name', agent_name)]
stub = sdk_service_pb2_grpc.SdkMgrServiceStub(channel)

# Global gNMI channel, used by multiple threads
#gnmi_options = [('username', 'admin'), ('password', 'admin')]
#gnmi_channel = grpc.insecure_channel(
#   'unix:///opt/srlinux/var/run/sr_gnmi_server', options = gnmi_options )

# Filter out whitespace and other possibly problematic characters
def clean(str):
    return str.replace(' ','_')

############################################################
## Subscribe to required event
## This proc handles subscription of: Config
############################################################
def Subscribe(stream_id, option):
    op = sdk_service_pb2.NotificationRegisterRequest.AddSubscription
    if option == 'cfg':
        entry = config_service_pb2.ConfigSubscriptionRequest()
        # entry.key.js_path = '.' + agent_name
        request = sdk_service_pb2.NotificationRegisterRequest(op=op, stream_id=stream_id, config=entry)

    subscription_response = stub.NotificationRegister(request=request, metadata=metadata)
    print('Status of subscription response for {}:: {}'.format(option, subscription_response.status))

############################################################
## Subscribe to all the events that Agent needs
############################################################
def Subscribe_Notifications(stream_id):
    '''
    Agent will receive notifications to what is subscribed here.
    '''
    if not stream_id:
        logging.info("Stream ID not sent.")
        return False

    # Subscribe to config changes, first
    Subscribe(stream_id, 'cfg')

############################################################
## Function to populate state of agent config
## using telemetry -- add/update info from state
############################################################
def Add_Telemetry(js_path, js_data):
    telemetry_stub = telemetry_service_pb2_grpc.SdkMgrTelemetryServiceStub(channel)
    telemetry_update_request = telemetry_service_pb2.TelemetryUpdateRequest()
    telemetry_info = telemetry_update_request.state.add()
    telemetry_info.key.js_path = js_path
    telemetry_info.data.json_content = js_data
    logging.info(f"Telemetry_Update_Request :: {telemetry_update_request}")
    telemetry_response = telemetry_stub.TelemetryAddOrUpdate(request=telemetry_update_request, metadata=metadata)
    return telemetry_response

##################################################################
## Proc to process the config Notifications received by auto_config_agent
## At present processing config from js_path containing agent_name
##################################################################
def Handle_Notification(obj, state):
    if obj.HasField('config'):
        logging.info(f"GOT CONFIG :: {obj.config.key.js_path}")
        if agent_name in obj.config.key.js_path:
            logging.info(f"Got config for agent, now will handle it :: \n{obj.config}\
                            Operation :: {obj.config.op}\nData :: {obj.config.data.json}")
            if obj.config.op == 2:
                logging.info(f"Delete context-agent cli scenario")
                # if file_name != None:
                #    Update_Result(file_name, action='delete')
                response=stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
                logging.info( f'Handle_Config: Unregister response:: {response}' )
                state = State() # Reset state, works?
            else:
                # Don't replace ' in filter expressions
                json_acceptable_string = obj.config.data.json # .replace("'", "\"")
                data = json.loads(json_acceptable_string)

                if obj.config.key.js_path == ".context_agent.enrich":

                    name = obj.config.key.keys[0]
                    if name in state.observations:
                       state.observations[ name ].update( data['enrich'] )
                    else:
                       state.observations[ name ] = data['enrich']
                elif obj.config.key.js_path == ".context_agent.enrich.collect":
                    name = obj.config.key.keys[0]
                    path = obj.config.key.keys[1]  # Path to collect
                    if name in state.observations:
                       if 'collect' in state.observations[ name ]:
                         state.observations[ name ]['collect'].update( { path : data } )
                       else:
                         state.observations[ name ]['collect'] = { path: data }
                    else:
                       logging.info( f"Check insertion order: {name}" )
                       state.observations[ name ] = { 'collect' : { path: data } }
                else:
                    logging.warning( f"Unhandled path: {obj.config.key.js_path}" )
                return True
        elif obj.config.key.js_path == ".commit.end":
           if state.observations != {}:
              MonitoringThread( state.observations ).start()

    else:
        logging.info(f"Unexpected notification : {obj}")

    # dont subscribe to LLDP now
    return False


# 2021-8-24 Yang paths in gNMI UPDATE events can have inconsistent ordering of keys, in the same update (!):
# 'network-instance[name=overlay]/bgp-rib/ipv4-unicast/rib-in-out/rib-in-post/routes[neighbor=192.168.127.3][prefix=10.10.10.10/32]/last-modified'
# 'network-instance[name=overlay]/bgp-rib/ipv4-unicast/rib-in-out/rib-in-post/routes[prefix=10.10.10.10/32][neighbor=192.168.127.3]/tie-break-reason
#
#path_key_regex = re.compile( '^(.*)\[([0-9a-zA-Z.-]+=[^]]+)\]\[([0-9a-zA-Z.-]+=[^]]+)\](.*)$' )
#def normalize_path(path):
#    double_index = path_key_regex.match( path )
#    if double_index:
#        g = double_index.groups()
#        if g[1] > g[2]:
#           return f"{g[0]}[{g[2]}][{g[1]}]{g[3]}"
#    return path # unmodified

#
# Runs as a separate thread
#
from threading import Thread
class MonitoringThread(Thread):
   def __init__(self, observations):
       Thread.__init__(self)
       self.observations = observations
       # self.startup_delay = startup_delay
       # Check that gNMI is connected now
       # grpc.channel_ready_future(gnmi_channel).result(timeout=5)


   def run(self):

      # Create per-thread gNMI stub, using a global channel
      # gnmi_stub = gNMIStub( gnmi_channel )
    try:
      logging.info( f"MonitoringThread: {self.observations}")

      # TODO expand paths like in opergroup agent, to allow regex generators
      subscribe = {
        'subscription': [
            {
                'path': value['path']['value'],
                # 'mode': 'on_change'
                'mode': 'on_change' if value['sample_period']['value'] == '0' else 'sample',
                'sample_interval':  int(value['sample_period']['value']) * 1000000000 # in ns
            } for key,value in self.observations.items()
        ],
        'use_aliases': False,
        # 'updates_only': True, # Optional
        'mode': 'stream',
        'encoding': 'json'
      }
      logging.info( f"MonitoringThread subscription: {subscribe}")

      # Build lookup map
      lookup = {}
      regexes = []

      def fix(regex):
          return regex.replace('*','.*').replace('[','\[').replace(']','\]')

      for name,atts in self.observations.items():
          path = atts['path']['value']     # normalized in pygnmi patch
          obj = { 'name': name, 'history': {}, 'last_known': {}, 'prev_known': {}, **atts }
          # range_match = re.match("(.*)(\\[\d+[-]\d+\\])(.*)",path)

          if '*' in path:
             regexes.append( (re.compile( fix(path) ),obj) )
          else:
             lookup[ path ] = obj

      logging.info( f"Built lookup map: {lookup} regexes={regexes} for sub={subscribe}" )

      def find_regex( path ):
        for r,o in regexes:
          if r.match( path ):
            return o, r.pattern
        return None,None

      def update_history( ts_ns, o, key, updates ):
          history = o['history'][ key ] if key in o['history'] else {}
          if 'history_window' in o['conditions']:
            window = ts_ns - int(o['conditions']['history_window']['value']) * 1000000000 # X seconds ago
          else:
            window = 0

          if 'history_items' in o['conditions']:
            max_items = int(o['conditions']['history_items']['value'])
          else:
            max_items = 0

          for path, val in updates:
             subitem = history[path] if path in history else []
             if window>0:
                if subitem==[]:
                   subitem = [(window,"<MISSING>")] # Start as MISSING
                else:
                    ts1, first = subitem[0]
                    ts2, last = subitem[-1]
                    if (ts2<window and last=="<MISSING>"):
                        # Move <MISSING> to edge of window, rest gets flushed
                        subitem = [ (window,last) ]
                    elif ts1!=ts2 and ts1<window and first=="<MISSING>":
                        if subitem[1][0] > window:
                           subitem = [ (window,first) ] + subitem[1:] # Move it along
                subitem = [ (ts,val) for ts,val in subitem if ts>=window ]
             if max_items>0:
                subitem = subitem[ -max_items: ]
             subitem.append( (ts_ns,val) )
             history[ path ] = subitem
          o['history'][ key ] = history

          # Return aggregated regex path history
          logging.info( f'update_history key={key} max={max_items} window={window} -> returning { history[ key ] }' )
          return history[ key ]

      # with Namespace('/var/run/netns/srbase-mgmt', 'net'):
      with gNMIclient(target=('unix:///opt/srlinux/var/run/sr_gnmi_server',57400),
                            username="admin",password="",
                            insecure=True, debug=False) as c:
        logging.info( f"gNMIclient: subscribe={subscribe}" )

        #
        # TODO check why this hangs for '/bfd/network-instance[name=default]/peer/oper-state'
        #
        telemetry_stream = c.subscribe(subscribe=subscribe)
        for m in telemetry_stream:
          if m.HasField('update'): # both update and delete events
              # Filter out only toplevel events
              parsed = telemetryParser(m)
              logging.info(f"gNMI change event :: {parsed}")
              update = parsed['update']
              if update['update']:
                  logging.info( f"Update: {update['update']}")

                  # For entries with a 'count' regex, count unique values in this update
                  # unique_count_o = None
                  # unique_count_matches = {}
                  for u in update['update']:

                      # Ignore any updates without 'val'
                      if 'val' not in u:
                          continue;

                      key = '/' + u['path'] # pygnmi strips '/'
                      regex = None
                      if key in lookup:
                         o = lookup[ key ]
                      elif regexes!=[]:
                         o, regex = find_regex( key )
                         if o is None:
                            logging.info( f"No matching regex found - skipping: '{key}' = {u['val']}" )
                            continue

                      else:
                         logging.info( f"No matching key found and no regexes - skipping: '{key}' = {u['val']}" )
                         continue

                      value = u['val']

                      # Use regex to aggregate history values (common path)
                      history_key = regex if regex is not None else key

                      # Add regex='val' and path='val' as implicit reported value? Just key=val for now
                      updates = [ (key,value) ] + ( [ (history_key,value) ] if regex else [] )

                      if 'conditions' in o: # Should be the case always
                        # To group subscriptions matching multiple paths, can collect by custom regex 'index'
                        # index = key
                        #if 'index' in o['conditions']:
                        #    _re = o['conditions']['index']['value']
                        #    _i = re.match( _re, key)
                        #    if _i and len(_i.groups()) > 0:
                        #      index = _i.groups()[0]
                        #    else:
                        #      logging.error( f"Error applying 'index' regex: {_re} to {key}" )
                        o['prev_known'][ key ] = o['last_known'][ key ] if key in o['last_known'] else 0
                        o['last_known'][ key ] = u['val']

                        # Helper function
                        def last_known_ints():
                          # List over all subpaths matching this observation
                          vs = list(map(int,o['last_known'].values()))
                          return vs if vs!=[] else [0]

                        def history_ints():
                          # List over a single path's history
                          history = o['history'][history_key] if history_key in o['history'] else {}
                          # history = { path -> (ts,val) } ??
                          logging.info( f"history_ints: {history}" )
                          values = history[history_key] if history_key in history else [(0,0)]

                          # Could calculate diffs: [ abs(vs[n]-vs[n-1]) for n in range(1,len(vs)) ]
                          return [ int(v) for t,v in values ]

                        def max_in_history(value_if_no_history=0):
                            hist = history_ints()
                            return max(hist) if hist!=[] else int(value_if_no_history)

                        def avg_in_history(value_if_no_history=0):
                            hist = history_ints()
                            return (sum(hist)/len(hist)) if hist!=[] else int(value_if_no_history)

                        def max_or_0(vals,x=0):
                            return max(vals) if vals!=[] else x

                        def min_or_0(vals,x=0):
                            return min(vals) if vals!=[] else x

                        def last_known_deltas():
                            ds = [ abs(int(v)-int(o['prev_known'][k])) for k,v in o['last_known'].items() ]
                            return ds if ds!=[] else [0] # Allow min/max to work

                        _globals = { "ipaddress" : ipaddress }
                        _locals  = { "_" : u['val'], **o,
                                     "last_known_ints": last_known_ints,
                                     "history_ints": history_ints,
                                     "max_in_history": max_in_history,
                                     "avg_in_history": avg_in_history,
                                     "max_or_0": max_or_0,
                                     "min_or_0": min_or_0,
                                     "last_known_deltas": last_known_deltas,
                                   }

                        # Custom value calculation, before filter
                        if 'value' in o['conditions']:
                          value_exp = o['conditions']['value']['value']
                          try:
                             value = eval( value_exp, _globals, _locals )
                             updates.append( (value_exp,value) )
                          except Exception as e:
                             logging.error( f"Custom value {value_exp} failed: {e}")
                        else:
                          updates.append( ('value',value) )

                        # logging.info( f"Evaluate any filters: {o}" )
                        if 'filter' in o['conditions']:
                          filter = o['conditions']['filter']['value']
                          _locals.update( { 'value': value } )
                          try:
                            if not eval( filter, _globals, _locals ):
                              logging.info( f"Filter {filter} with _='{u['val']}' value='{value}' = False, skipping..." )
                              # Update_Filtered(o, int( update['timestamp'] ), key, value )
                              continue;
                          except Exception as ex:
                            logging.error( f"Exception during filter {filter}: {ex}" )

                      # For sampled state, reset the interval timer
                      sample_period = o['conditions']['sample_period']['value']
                      if sample_period != "0":
                          if 'timer' in o:
                              if key in o['timer']:
                                 o['timer'][key].cancel()
                          else:
                              o['timer'] = {}
                          def missing_sample(_o,_key,_hist_key,_ts_ns,_sample_period):
                             logging.info( f"Missing sample timer: {_key} aggregate history_key={_hist_key}" )
                             _i = _key.rindex('/') + 1
                             _history = update_history( _ts_ns, _o, _hist_key, [ (_key,"<MISSING>"),( _hist_key,"<MISSING>") ] )
                             # Update_Observation( _o, _ts_ns, f"{_key[_i:]}=missing sample={_sample_period}",
                             #                     int(_sample_period), [(_hist_key,"<MISSING>")], _history, path=_key )

                          ts_ns = int( update['timestamp'] ) + 1000000000 * int(sample_period)
                          timer = o['timer'][key] = Timer( int(sample_period) + 1, missing_sample,
                                                           [o,key,history_key,ts_ns,sample_period] )
                          timer.start()

                        # Also process any 'count' regex, could compile once
                        # if 'count' in o['conditions']:
                        #      count_regex = o['conditions']['count']['value']
                        #      unique_count_o = o
                        #      # logging.info( f"Match {count_regex} against {key}" )
                        #      m = re.match( count_regex, key )
                        #      if m:
                        #        # logging.info( f"Matches found: {m.groups()}" )
                        #        for g in m.groups():
                        #           unique_count_matches[ g ] = True
                        #        continue  # Skip updating invidivual values

                      reports = o['reports']
                      if reports != []:
                        def _lookup(param): # match looks like {x}
                           _var = param[1:-1]  # Strip '{' and '}'
                           _val = re.match( f".*\[{_var}=([^]]+)\].*", key)
                           if _val:
                              return _val.groups()[0]
                           else:
                              logging.error( f"Unable to resolve {param} in {key}, returning '*'" )
                              return "*" # Avoid causing gNMI errors

                        # Substitute any {key} values in paths
                        resolved_paths = [ re.sub('\{(.*)\}', lambda m: _lookup(m.group()), path) for path in reports ]

                        data = c.get(path=resolved_paths, encoding='json_ietf')
                        logging.info( f"Reports:{data} val={value}" )
                        # update Telemetry, iterate
                        i = 0
                        for n in data['notification']:
                           if 'update' in n: # Update is empty when path is invalid
                             for u2 in n['update']:
                                updates.append( (u2['path'],u2['val']) )
                           else:
                             # Assumes updates are in same order
                             updates.append( (resolved_paths[i], 'GET failed') )
                           i = i + 1

                      # Update historical data, indexed by key. Remove old entries
                      history = update_history( int( update['timestamp'] ), o, history_key, updates )
                      s_index = key.rindex('/') + 1
                      sample = o['conditions']['sample_period']['value']
                      # try:
                    #      Update_Observation( o, int( update['timestamp'] ), f"{key[s_index:]}={value} sample={sample}",
                    #                          int(sample), updates, history,
                    #                          path=key, value=value ) # Use actual path for event reporting
                      # except Exception as ex:
                    #      traceback_str = ''.join(traceback.format_tb(ex.__traceback__))
                    #      logging.error( f"Exception while updating telemetry - EXITING: {ex} ~ {traceback_str}" )

                    #      # Force agent to exit
                    #      Exit_Gracefully(0,0)

                #  if unique_count_o is not None:
                #      vals = sorted( list(unique_count_matches.keys()) )
                #      updates = [("count",vals)]
                #      series = update_history( int( update['timestamp'] ), unique_count_o, "count", updates )
                #      cur_set = set( v for ts,vs in series for v in vs )
                #      logging.info( f"Reporting unique values: {unique_count_matches} -> cur_set={cur_set}" )
                #      summary = [( "count", sorted(list(cur_set)) )]
                #      sample = unique_count_o['conditions']['sample_period']['value']
                #      Update_Observation( unique_count_o, int( update['timestamp'] ), f"count={summary}", int(sample), summary, series )

    except Exception as e:
       traceback_str = ''.join(traceback.format_tb(e.__traceback__))
       logging.error(f'Exception caught in gNMI :: {e} stack:{traceback_str}')
    except:
       logging.error(f"Unexpected error: {sys.exc_info()[0]}")

    logging.info("Leaving gNMI subscribe loop")

class State(object):
    def __init__(self):
        self.startup_delay = 0
        from collections import OrderedDict # From 3.6 default dict should be ordered
        self.observations = OrderedDict() # Map of [name] -> { paths: [], reports: [] }

    def __str__(self):
        return str(self.__class__) + ": " + str(self.__dict__)

##################################################################################################
## This is the main proc where all processing for docter_agent starts.
## Agent registration, notification registration, Subscrition to notifications.
## Waits on the subscribed Notifications and once any config is received, handles that config
## If there are critical errors, Unregisters the fib_agent gracefully.
##################################################################################################
def Run():
    # optional agent_liveliness=<seconds> to have system kill unresponsive agents
    response = stub.AgentRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
    logging.info(f"Registration response : {response.status}")

    request=sdk_service_pb2.NotificationRegisterRequest(op=sdk_service_pb2.NotificationRegisterRequest.Create)
    create_subscription_response = stub.NotificationRegister(request=request, metadata=metadata)
    stream_id = create_subscription_response.stream_id
    logging.info(f"Create subscription response received. stream_id : {stream_id}")

    Subscribe_Notifications(stream_id)

    sub_stub = sdk_service_pb2_grpc.SdkNotificationServiceStub(channel)
    stream_request = sdk_service_pb2.NotificationStreamRequest(stream_id=stream_id)
    stream_response = sub_stub.NotificationStream(stream_request, metadata=metadata)

    # Grafana_Test()
    # Show_Dummy_Health( controlplane="green", links="orange" )

    state = State()
    count = 1
    lldp_subscribed = False
    try:
        for r in stream_response:
            logging.info(f"Count :: {count}  NOTIFICATION:: \n{r.notification}")
            count += 1
            for obj in r.notification:
                if Handle_Notification(obj, state) and not lldp_subscribed:
                   # Subscribe(stream_id, 'lldp')
                   # Subscribe(stream_id, 'route')
                   lldp_subscribed = True

                # Program router_id only when changed
                # if state.router_id != old_router_id:
                #   gnmic(path='/network-instance[name=default]/protocols/bgp/router-id',value=state.router_id)
                logging.info(f'Updated state: {state}')

    except grpc._channel._Rendezvous as err:
        logging.info(f'GOING TO EXIT NOW: {err}')

    except Exception as e:
        logging.error(f'Exception caught :: {e}')
        #if file_name != None:
        #    Update_Result(file_name, action='delete')
        try:
            response = stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
            logging.error(f'Run try: Unregister response:: {response}')
        except grpc._channel._Rendezvous as err:
            logging.info(f'GOING TO EXIT NOW: {err}')
            sys.exit()
        return True
    sys.exit()
    return True
############################################################
## Gracefully handle SIGTERM signal
## When called, will unregister Agent and gracefully exit
############################################################
def Exit_Gracefully(signum, frame):
    logging.info("Caught signal :: {}\n will unregister docter agent".format(signum))
    try:
        response=stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
        logging.error('try: Unregister response:: {}'.format(response))
        sys.exit()
    except grpc._channel._Rendezvous as err:
        logging.info('GOING TO EXIT NOW: {}'.format(err))
        sys.exit()

##################################################################################################
## Main from where the Agent starts
## Log file is written to: /var/log/srlinux/stdout/<agent_name>.log
## Signals handled for graceful exit: SIGTERM
##################################################################################################
if __name__ == '__main__':
    # hostname = socket.gethostname()
    stdout_dir = '/var/log/srlinux/stdout' # PyTEnv.SRL_STDOUT_DIR
    signal.signal(signal.SIGTERM, Exit_Gracefully)
    if not os.path.exists(stdout_dir):
        os.makedirs(stdout_dir, exist_ok=True)
    log_filename = f'{stdout_dir}/{agent_name}.log'
    logging.basicConfig(
      handlers=[RotatingFileHandler(log_filename, maxBytes=3000000,backupCount=5)],
      format='%(asctime)s,%(msecs)03d %(name)s %(levelname)s %(message)s',
      datefmt='%H:%M:%S', level=logging.INFO)
    logging.info("START TIME :: {}".format(datetime.now()))
    if Run():
        logging.info('Docter agent unregistered')
    else:
        logging.info('Should not happen')
