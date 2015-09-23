# Copyright (C) 2013 Nippon Telegraph and Telephone Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import logging
import warnings

import socket
import struct

import json
from webob import Response
import requests
import urllib3.contrib.pyopenssl

from ryu import cfg
from ryu.app.wsgi import ControllerBase
from ryu.app.wsgi import WSGIApplication
from ryu.base import app_manager
from ryu.controller import dpset
from ryu.controller import ofp_event
from ryu.controller.handler import set_ev_cls
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.exception import OFPUnknownVersion
from ryu.exception import RyuException
from ryu.lib import dpid as dpid_lib
from ryu.lib import hub
from ryu.lib import mac as mac_lib
from ryu.lib import addrconv
from ryu.lib.packet import arp
from ryu.lib.packet import dhcp
from ryu.lib.packet import ethernet
from ryu.lib.packet import icmp
from ryu.lib.packet import in_proto
from ryu.lib.packet import ipv4
from ryu.lib.packet import packet
from ryu.lib.packet import tcp
from ryu.lib.packet import udp
from ryu.lib.packet import vlan
from ryu.ofproto import ether
from ryu.ofproto import inet
from ryu.ofproto import ofproto_v1_0
from ryu.ofproto import ofproto_v1_2
from ryu.ofproto import ofproto_v1_3


#=============================
#          REST API
#=============================
#
#  Note: specify switch and vlan group, as follows.
#   {switch_id} : 'all' or switchID
#   {vlan_id}   : 'all' or vlanID
#
#
## 1. get address data and routing data.
#
# * get data of no vlan
# GET /router/{switch_id}
#
# * get data of specific vlan group
# GET /router/{switch_id}/{vlan_id}
#
#
## 2. set address data or routing data.
#
# * set data of no vlan
# POST /router/{switch_id}
#
# * set data of specific vlan group
# POST /router/{switch_id}/{vlan_id}
#
#  case1: set address data.
#    parameter = {"address": "A.B.C.D/M"}
#  case2-1: set static route.
#    parameter = {"destination": "A.B.C.D/M", "gateway": "E.F.G.H"}
#  case2-2: set default route.
#    parameter = {"gateway": "E.F.G.H"}
#  case2-3: set static route for a specific address range.
#    parameter = {"destination": "A.B.C.D/M", "gateway": "E.F.G.H", "address_id": "<int>"}
#  case2-4: set default route for a specific address range.
#    parameter = {"gateway": "E.F.G.H", "address_id": "<int>"}
#  case3: set DHCP server for a VLAN
#    parameter = {"dhcp_servers": [ "A.B.C.D", "E.F.G.H" ]}
#
#
## 3. delete address data or routing data.
#
# * delete data of no vlan
# DELETE /router/{switch_id}
#
# * delete data of specific vlan group
# DELETE /router/{switch_id}/{vlan_id}
#
#  case1: delete address data.
#    parameter = {"address_id": "<int>"} or {"address_id": "all"}
#  case2: delete routing data.
#    parameter = {"route_id": "<int>"} or {"route_id": "all"}
#
#


UINT16_MAX = 0xffff
UINT32_MAX = 0xffffffff
UINT64_MAX = 0xffffffffffffffff

ETHERNET = ethernet.ethernet.__name__
VLAN = vlan.vlan.__name__
IPV4 = ipv4.ipv4.__name__
ARP = arp.arp.__name__
ICMP = icmp.icmp.__name__
TCP = tcp.tcp.__name__
UDP = udp.udp.__name__
DHCP = dhcp.dhcp.__name__

MAX_SUSPENDPACKETS = 3  # Maximum number of suspended packets awaiting send.

ARP_REPLY_TIMER = 10  # sec
OFP_REPLY_TIMER = 1.0  # sec
CHK_ROUTING_TBL_INTERVAL = 30  # Seconds before cyclically checking reachability of all switch-defined routers

SWITCHID_PATTERN = dpid_lib.DPID_PATTERN + r'|all'
VLANID_PATTERN = r'[0-9]{1,4}|all'

VLANID_NONE = 0
VLANID_MIN = 2
VLANID_MAX = 4094

DHCP_SERVER_PORT = 67
DHCP_CLIENT_PORT = 68

COOKIE_DEFAULT_ID = 0
COOKIE_SHIFT_VLANID = 32
COOKIE_SHIFT_ROUTEID = 16

INADDR_ANY_BASE = '0.0.0.0'
INADDR_ANY_MASK = '0'
INADDR_ANY = INADDR_ANY_BASE + '/' + INADDR_ANY_MASK

INADDR_BROADCAST_BASE = '255.255.255.255'
INADDR_BROADCAST_MASK = '32'
INADDR_BROADCAST = INADDR_BROADCAST_BASE + '/' + INADDR_BROADCAST_MASK

IDLE_TIMEOUT = 300  # sec
DEFAULT_TTL = 64

REST_COMMAND_RESULT = 'command_result'
REST_RESULT = 'result'
REST_DETAILS = 'details'
REST_OK = 'success'
REST_NG = 'failure'
REST_ALL = 'all'
REST_SWITCHID = 'switch_id'
REST_VLANID = 'vlan_id'
REST_NW = 'internal_network'
REST_ADDRESSID = 'address_id'
REST_ADDRESS = 'address'
REST_ROUTEID = 'route_id'
REST_ROUTE = 'route'
REST_DESTINATION = 'destination'
REST_GATEWAY = 'gateway'
REST_GATEWAY_MAC = 'gateway_mac'
REST_SOURCE = 'source'
REST_BARE = 'bare'
REST_DHCP = 'dhcp_servers'

PRIORITY_VLAN_SHIFT = 1000
PRIORITY_NETMASK_SHIFT = 32

PRIORITY_ARP_HANDLING = 1
PRIORITY_DEFAULT_ROUTING = 1
PRIORITY_ADDRESSED_DEFAULT_ROUTING = 2
PRIORITY_MAC_LEARNING = 3
PRIORITY_STATIC_ROUTING = 3
PRIORITY_ADDRESSED_STATIC_ROUTING = 4
PRIORITY_IMPLICIT_ROUTING = 5
PRIORITY_L2_SWITCHING = 6
PRIORITY_IP_HANDLING = 7

PRIORITY_TYPE_ROUTE = 'priority_route'

CONF = cfg.CONF
switchboard_configuration_group = 'switchboard'
switchboard_stateurl_opt = cfg.StrOpt('state_url',
                                      default = 'https://switchboard.oit.duke.edu/sdn_callback/restore_state',
                                      help = 'URL for accessing SwitchBoard knowledge base')
switchboard_username_opt = cfg.StrOpt('username',
                                      default = 'username',
                                      help='Username for accessing SwitchBoard knowledge base')
switchboard_password_opt = cfg.StrOpt('password',
                                      default = 'password',
                                      help = 'Password for accessing SwitchBoard knowledge base')
CONF.register_opt(switchboard_stateurl_opt, group = switchboard_configuration_group)
CONF.register_opt(switchboard_username_opt, group = switchboard_configuration_group)
CONF.register_opt(switchboard_password_opt, group = switchboard_configuration_group)


def get_priority(priority_type, vid=0, route=None):
    log_msg = None
    priority = priority_type

    if priority_type == PRIORITY_TYPE_ROUTE:
        assert route is not None
        if route.dst_ip:
            if route.src_ip:
                priority_type = PRIORITY_ADDRESSED_STATIC_ROUTING
            else:
                priority_type = PRIORITY_STATIC_ROUTING
            priority = priority_type + route.dst_netmask
            log_msg = 'static routing'
        else:
            if route.src_ip:
                priority_type = PRIORITY_ADDRESSED_DEFAULT_ROUTING
            else:
                priority_type = PRIORITY_DEFAULT_ROUTING
            priority = priority_type
            log_msg = 'default routing'

    if vid or priority_type == PRIORITY_IP_HANDLING:
        priority += PRIORITY_VLAN_SHIFT

    if priority_type > PRIORITY_ADDRESSED_STATIC_ROUTING:
        priority += PRIORITY_NETMASK_SHIFT

    if log_msg is None:
        return priority
    else:
        return priority, log_msg


def get_priority_type(priority, vid):
    if vid:
        priority -= PRIORITY_VLAN_SHIFT
    return priority


class NotFoundError(RyuException):
    message = 'Router SW is not connected. : switch_id=%(switch_id)s'


class CommandFailure(RyuException):
    pass


class Plexus(app_manager.RyuApp):

    OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION,
                    ofproto_v1_2.OFP_VERSION,
                    ofproto_v1_3.OFP_VERSION]

    _CONTEXTS = {'dpset': dpset.DPSet,
                 'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(Plexus, self).__init__(*args, **kwargs)

        # logger configure
        PlexusController.set_logger(self.logger)

        wsgi = kwargs['wsgi']
        self.waiters = {}
        self.data = {'waiters': self.waiters}

        mapper = wsgi.mapper
        wsgi.registory['PlexusController'] = self.data
        requirements = {'switch_id': SWITCHID_PATTERN,
                        'vlan_id': VLANID_PATTERN}

        # For no vlan data
        path = '/router/{switch_id}'
        mapper.connect('router', path, controller=PlexusController,
                       requirements=requirements,
                       action='get_data',
                       conditions=dict(method=['GET']))
        mapper.connect('router', path, controller=PlexusController,
                       requirements=requirements,
                       action='set_data',
                       conditions=dict(method=['POST']))
        mapper.connect('router', path, controller=PlexusController,
                       requirements=requirements,
                       action='delete_data',
                       conditions=dict(method=['DELETE']))
        # For vlan data
        path = '/router/{switch_id}/{vlan_id}'
        mapper.connect('router', path, controller=PlexusController,
                       requirements=requirements,
                       action='get_vlan_data',
                       conditions=dict(method=['GET']))
        mapper.connect('router', path, controller=PlexusController,
                       requirements=requirements,
                       action='set_vlan_data',
                       conditions=dict(method=['POST']))
        mapper.connect('router', path, controller=PlexusController,
                       requirements=requirements,
                       action='delete_vlan_data',
                       conditions=dict(method=['DELETE']))

    @set_ev_cls(dpset.EventDP, dpset.DPSET_EV_DISPATCHER)
    def datapath_handler(self, ev):
        if ev.enter:
            PlexusController.register_router(ev.dp)
        else:
            PlexusController.unregister_router(ev.dp)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        datapath.n_tables = ev.msg.n_tables

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        PlexusController.packet_in_handler(ev.msg)

    def _stats_reply_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath

        if (dp.id not in self.waiters
                or msg.xid not in self.waiters[dp.id]):
            return
        event, msgs = self.waiters[dp.id][msg.xid]
        msgs.append(msg)

        if ofproto_v1_3.OFP_VERSION == dp.ofproto.OFP_VERSION:
            more = dp.ofproto.OFPMPF_REPLY_MORE
        else:
            more = dp.ofproto.OFPSF_REPLY_MORE
        if msg.flags & more:
            return
        del self.waiters[dp.id][msg.xid]
        event.set()

    # for OpenFlow version1.0
    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def stats_reply_handler_v1_0(self, ev):
        self._stats_reply_handler(ev)

    # for OpenFlow version1.2/1.3
    @set_ev_cls(ofp_event.EventOFPStatsReply, MAIN_DISPATCHER)
    def stats_reply_handler_v1_2(self, ev):
        self._stats_reply_handler(ev)

    #TODO: Update routing table when port status is changed.


# REST command template
def rest_command(func):
    def _rest_command(*args, **kwargs):
        try:
            msg = func(*args, **kwargs)
            return Response(content_type='application/json',
                            body=json.dumps(msg))

        except SyntaxError as e:
            status = 400
            details = e.msg
        except (ValueError, NameError) as e:
            status = 400
            details = e.message

        except NotFoundError as msg:
            status = 404
            details = str(msg)

        msg = {REST_RESULT: REST_NG,
               REST_DETAILS: details}
        return Response(status=status, body=json.dumps(msg))

    return _rest_command

class RouterLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return '[DPID %16s] %s' % (self.extra['sw_id'], msg), kwargs


class PlexusController(ControllerBase):

    _ROUTER_LIST = {}
    _LOGGER = None

    def __init__(self, req, link, data, **config):
        super(PlexusController, self).__init__(req, link, data, **config)
        self.waiters = data['waiters']

    @classmethod
    def set_logger(cls, logger):
        cls._LOGGER = logger

    @classmethod
    def register_router(cls, dp):
        logger = RouterLoggerAdapter(cls._LOGGER, {'sw_id': dpid_lib.dpid_to_str(dp.id)})
        try:
            router = Router(dp, logger)
        except OFPUnknownVersion as message:
            logger.error(str(message))
            return
        cls._ROUTER_LIST.setdefault(dp.id, router)
        logger.info('Join as router.')

        logger.info('Requesting configuration from Switchboard.')
        try:
            urllib3.contrib.pyopenssl.inject_into_urllib3()
            payload = {'rest_caller_id': CONF.switchboard.username, 'rest_caller_pw': CONF.switchboard.password}
            r = requests.get(CONF.switchboard.state_url, params=payload)
        except:
            logger.error('Error in retrieving Switchboard configuration!')
            return

    @classmethod
    def unregister_router(cls, dp):
        if dp.id in cls._ROUTER_LIST:
            cls._ROUTER_LIST[dp.id].delete()
            del cls._ROUTER_LIST[dp.id]

            logger = RouterLoggerAdapter(cls._LOGGER, {'sw_id': dpid_lib.dpid_to_str(dp.id)})
            logger.info('Leave router.')

    @classmethod
    def packet_in_handler(cls, msg):
        dp_id = msg.datapath.id
        if dp_id in cls._ROUTER_LIST:
            router = cls._ROUTER_LIST[dp_id]
            router.packet_in_handler(msg)

    # GET /router/{switch_id}
    @rest_command
    def get_data(self, req, switch_id, **_kwargs):
        return self._access_router(switch_id, VLANID_NONE,
                                   'get_data', req.body)

    # GET /router/{switch_id}/{vlan_id}
    @rest_command
    def get_vlan_data(self, req, switch_id, vlan_id, **_kwargs):
        return self._access_router(switch_id, vlan_id,
                                   'get_data', req.body)

    # POST /router/{switch_id}
    @rest_command
    def set_data(self, req, switch_id, **_kwargs):
        return self._access_router(switch_id, VLANID_NONE,
                                   'set_data', req.body)

    # POST /router/{switch_id}/{vlan_id}
    @rest_command
    def set_vlan_data(self, req, switch_id, vlan_id, **_kwargs):
        return self._access_router(switch_id, vlan_id,
                                   'set_data', req.body)

    # DELETE /router/{switch_id}
    @rest_command
    def delete_data(self, req, switch_id, **_kwargs):
        return self._access_router(switch_id, VLANID_NONE,
                                   'delete_data', req.body)

    # DELETE /router/{switch_id}/{vlan_id}
    @rest_command
    def delete_vlan_data(self, req, switch_id, vlan_id, **_kwargs):
        return self._access_router(switch_id, vlan_id,
                                   'delete_data', req.body)

    def _access_router(self, switch_id, vlan_id, func, rest_param):
        rest_message = []
        routers = self._get_router(switch_id)
        param = eval(rest_param) if rest_param else {}
        for router in routers.values():
            function = getattr(router, func)
            data = function(vlan_id, param, self.waiters)
            rest_message.append(data)

        return rest_message

    def _get_router(self, switch_id):
        routers = {}

        if switch_id == REST_ALL:
            routers = self._ROUTER_LIST
        else:
            sw_id = dpid_lib.str_to_dpid(switch_id)
            if sw_id in self._ROUTER_LIST:
                routers = {sw_id: self._ROUTER_LIST[sw_id]}

        if routers:
            return routers
        else:
            raise NotFoundError(switch_id=switch_id)


class Router(dict):
    def __init__(self, dp, logger):
        super(Router, self).__init__()
        self.dp = dp
        self.dpid_str = dpid_lib.dpid_to_str(dp.id)
        self.sw_id = {'sw_id': self.dpid_str}
        self.logger = logger

        # FIXME: Can silence warning about using ports here.
        # Probably not the right fix though.
        #with warnings.catch_warnings():
        #    warnings.simplefilter('ignore')
        #    self.port_data = PortData(dp.ports)
        self.port_data = PortData(dp.ports)

        ofctl = OfCtl.factory(dp, logger)
        cookie = COOKIE_DEFAULT_ID

        # Set SW config: TTL error packet in (for OFPv1.2/1.3)
        ofctl.set_sw_config_for_ttl()

        # Clear existing flows:
        ofctl.clear_flows()
        self.logger.info('Clearing pre-existing flows [cookie=0x%x]', cookie)

        # Set flow: ARP handling (packet in)
        priority = get_priority(PRIORITY_ARP_HANDLING)
        ofctl.set_packetin_flow(cookie, priority, dl_type=ether.ETH_TYPE_ARP)
        self.logger.info('Set ARP handling (packet in) flow [cookie=0x%x]', cookie)

        # Set VlanRouter for vid=None.
        vlan_router = VlanRouter(VLANID_NONE, dp, self.port_data, logger)
        self[VLANID_NONE] = vlan_router

        # Start cyclic routing table check.
        self.thread = hub.spawn(self._cyclic_update_routing_tbls)
        self.logger.info('Start cyclic routing table update.')

    def delete(self):
        hub.kill(self.thread)
        self.thread.wait()
        self.logger.info('Stop cyclic routing table update.')

    def _get_vlan_router(self, vlan_id):
        vlan_routers = []

        if vlan_id == REST_ALL:
            vlan_routers = self.values()
        else:
            vlan_id = int(vlan_id)
            if (vlan_id != VLANID_NONE and
                    (vlan_id < VLANID_MIN or VLANID_MAX < vlan_id)):
                msg = 'Invalid {vlan_id} value. Set [%d-%d]'
                raise ValueError(msg % (VLANID_MIN, VLANID_MAX))
            elif vlan_id in self:
                vlan_routers = [self[vlan_id]]

        return vlan_routers

    def _add_vlan_router(self, vlan_id, bare=False):
        vlan_id = int(vlan_id)
        if vlan_id not in self:
            vlan_router = VlanRouter(vlan_id, self.dp, self.port_data,
                                     self.logger, bare)
            self[vlan_id] = vlan_router
        return self[vlan_id]

    def _del_vlan_router(self, vlan_id, waiters):
        # Remove unnecessary VlanRouter.
        if vlan_id == VLANID_NONE:
            return

        vlan_router = self[vlan_id]
        # GC empty subnet routing tables before attempting the delete.
        vlan_router.policy_routing_tbl.gc_subnet_tables()

        if (len(vlan_router.address_data) == 0
                and len(vlan_router.policy_routing_tbl) == 1
                and len(vlan_router.policy_routing_tbl[INADDR_ANY]) == 0):
            vlan_router.delete(waiters)
            del self[vlan_id]

    def get_data(self, vlan_id, dummy1, dummy2):
        vlan_routers = self._get_vlan_router(vlan_id)
        if vlan_routers:
            msgs = [vlan_router.get_data() for vlan_router in vlan_routers]
        else:
            msgs = [{REST_VLANID: vlan_id}]

        return {REST_SWITCHID: self.dpid_str,
                REST_NW: msgs}

    def set_data(self, vlan_id, param, waiters):
        vlan_routers = self._get_vlan_router(vlan_id)
        if not vlan_routers:
            if REST_BARE in param:
                bare = param[REST_BARE]
                try:
                    bare = bool(bare)
                except ValueError as e:
                    err_msg = 'Invalid [%s] value. %s'
                    raise ValueError(err_msg % (REST_BARE, e.message))
            vlan_routers = [self._add_vlan_router(vlan_id, bare)]

        msgs = []
        for vlan_router in vlan_routers:
            if not vlan_router.bare:
                try:
                    msg = vlan_router.set_data(param)
                    msgs.append(msg)
                    if msg[REST_RESULT] == REST_NG:
                        # Data setting is failure.
                        self._del_vlan_router(vlan_router.vlan_id, waiters)
                except ValueError as err_msg:
                    # Data setting is failure.
                    self._del_vlan_router(vlan_router.vlan_id, waiters)
                    raise err_msg

        return {REST_SWITCHID: self.dpid_str,
                REST_COMMAND_RESULT: msgs}

    def delete_data(self, vlan_id, param, waiters):
        msgs = []
        vlan_routers = self._get_vlan_router(vlan_id)
        if vlan_routers:
            for vlan_router in vlan_routers:
                msg = vlan_router.delete_data(param, waiters)
                if msg:
                    msgs.append(msg)
                # Check unnecessary VlanRouter.
                self._del_vlan_router(vlan_router.vlan_id, waiters)
        if not msgs:
            msgs = [{REST_RESULT: REST_NG,
                     REST_DETAILS: 'Data is nothing.'}]

        return {REST_SWITCHID: self.dpid_str,
                REST_COMMAND_RESULT: msgs}

    def packet_in_handler(self, msg):
        pkt = None
        try:
            pkt = packet.Packet(msg.data)
        except:
            return None 
        #TODO: Packet library convert to string
        #self.logger.debug('Packet in = %s', str(pkt), self.sw_id)
        header_list = dict((p.protocol_name, p)
                           for p in pkt.protocols if type(p) != str)
        if header_list:
            # Check vlan-tag
            vlan_id = VLANID_NONE
            if VLAN in header_list:
                vlan_id = header_list[VLAN].vid

            # Event dispatch
            if vlan_id in self:
                self[vlan_id].packet_in_handler(msg, header_list)
            else:
                self.logger.debug('Drop unknown vlan packet. [vlan_id=%d]', vlan_id)

    def _cyclic_update_routing_tbls(self):
        while True:
            # send ARP to all gateways.
            for vlan_router in self.values():
                vlan_router.send_arp_all_gw()
                hub.sleep(1)

            hub.sleep(CHK_ROUTING_TBL_INTERVAL)


class VlanRouter(object):
    def __init__(self, vlan_id, dp, port_data, logger, bare=False):
        super(VlanRouter, self).__init__()
        self.vlan_id = vlan_id
        self.dp = dp
        self.sw_id = {'sw_id': dpid_lib.dpid_to_str(dp.id)}
        self.logger = logger

        self.port_data = port_data
        self.address_data = AddressData()
        self.policy_routing_tbl = PolicyRoutingTable()
        self.packet_buffer = SuspendPacketList(self.send_icmp_unreach_error)
        self.ofctl = OfCtl.factory(dp, logger)
        self.bare = bare

        # Set flow: default route (drop), if VLAN is not "bare"
        if not self.bare:
            self._set_defaultroute_drop()

    def delete(self, waiters):
        # Delete flow.
        msgs = self.ofctl.get_all_flow(waiters)
        for msg in msgs:
            for stats in msg.body:
                vlan_id = VlanRouter._cookie_to_id(REST_VLANID, stats.cookie)
                if vlan_id == self.vlan_id:
                    self.ofctl.delete_flow(stats)

        assert len(self.packet_buffer) == 0

    @staticmethod
    def _cookie_to_id(id_type, cookie):
        if id_type == REST_VLANID:
            rest_id = cookie >> COOKIE_SHIFT_VLANID
        elif id_type == REST_ADDRESSID:
            rest_id = cookie & UINT32_MAX
        else:
            assert id_type == REST_ROUTEID
            rest_id = (cookie & UINT32_MAX) >> COOKIE_SHIFT_ROUTEID

        return rest_id

    def _id_to_cookie(self, id_type, rest_id):
        vid = self.vlan_id << COOKIE_SHIFT_VLANID

        if id_type == REST_VLANID:
            cookie = rest_id << COOKIE_SHIFT_VLANID
        elif id_type == REST_ADDRESSID:
            cookie = vid + rest_id
        else:
            assert id_type == REST_ROUTEID
            cookie = vid + (rest_id << COOKIE_SHIFT_ROUTEID)

        return cookie

    def _get_priority(self, priority_type, route=None):
        return get_priority(priority_type, vid=self.vlan_id, route=route)

    def _response(self, msg):
        if msg and self.vlan_id:
            msg.setdefault(REST_VLANID, self.vlan_id)
        return msg

    def get_data(self):
        address_data = self._get_address_data()
        routing_data = self._get_routing_data()

        data = {}
        if address_data[REST_ADDRESS]:
            data.update(address_data)
        if routing_data[REST_ROUTE]:
            data.update(routing_data)

        return self._response(data)

    def _get_address_data(self):
        address_data = []
        for value in self.address_data.values():
            default_gw = ip_addr_ntoa(value.default_gw)
            address = '%s/%d' % (default_gw, value.netmask)
            data = {REST_ADDRESSID: value.address_id,
                    REST_ADDRESS: address}
            address_data.append(data)
        return {REST_ADDRESS: address_data}

    def _get_routing_data(self):
        routing_data = []
        for table in self.policy_routing_tbl.values():
            for dst, route in table.items():
                gateway = ip_addr_ntoa(route.gateway_ip)
                source_addr = ip_addr_ntoa(route.src_ip)
                source = '%s/%d' % (source_addr, route.src_netmask)
                data = {REST_ROUTEID: route.route_id,
                        REST_DESTINATION: dst,
                        REST_GATEWAY: gateway,
                        REST_GATEWAY_MAC: route.gateway_mac,
                        REST_SOURCE: source}
                routing_data.append(data)
        return {REST_ROUTE: routing_data}

    def set_data(self, data):
        details = None

        try:
            # Set address data
            if REST_ADDRESS in data:
                address = data[REST_ADDRESS]
                address_id = self._set_address_data(address)
                details = 'Add address [address_id=%d]' % address_id
            # Set routing data
            elif REST_GATEWAY in data:
                gateway = data[REST_GATEWAY]
                address_id = None
                destination = INADDR_ANY

                if REST_DESTINATION in data:
                    destination = data[REST_DESTINATION]

                if REST_ADDRESSID in data:
                    address_id = data[REST_ADDRESSID]

                route_id = self._set_routing_data(destination, gateway, address_id)
                details = 'Add route [route_id=%d]' % route_id
            elif REST_DHCP in data:
                dhcp_servers = data[REST_DHCP]
                # FIXME: Placeholder for DHCP implementation.
                details = 'DHCP server(s) set as %r' % dhcp_servers

        except CommandFailure as err_msg:
            msg = {REST_RESULT: REST_NG, REST_DETAILS: str(err_msg)}
            return self._response(msg)

        if details is not None:
            msg = {REST_RESULT: REST_OK, REST_DETAILS: details}
            return self._response(msg)
        else:
            raise ValueError('Invalid parameter.')

    def _set_address_data(self, address):
        address = self.address_data.add(address)

        cookie = self._id_to_cookie(REST_ADDRESSID, address.address_id)

        # Set flow: host MAC learning (packet in)
        priority = self._get_priority(PRIORITY_MAC_LEARNING)
        self.ofctl.set_packetin_flow(cookie, priority,
                                     dl_type=ether.ETH_TYPE_IP,
                                     dl_vlan=self.vlan_id,
                                     dst_ip=address.nw_addr,
                                     dst_mask=address.netmask)
        log_msg = 'Set host MAC learning (packet in) flow [cookie=0x%x]'
        self.logger.info(log_msg, cookie)

        # set Flow: IP handling(PacketIn)
        priority = self._get_priority(PRIORITY_IP_HANDLING)
        self.ofctl.set_packetin_flow(cookie, priority,
                                     dl_type=ether.ETH_TYPE_IP,
                                     dl_vlan=self.vlan_id,
                                     dst_ip=address.default_gw)
        self.logger.info('Set IP handling (packet in) flow [cookie=0x%x]', cookie)

        # Send GARP
        self.send_arp_request(address.default_gw, address.default_gw)

        return address.address_id

    def _set_routing_data(self, destination, gateway, address_id=None):
        err_msg = 'Invalid [%s] value.' % REST_GATEWAY
        dst_ip = ip_addr_aton(gateway, err_msg=err_msg)
        address = self.address_data.get_data(ip=dst_ip)
        requested_address = None

        if address_id is not None:
            if address_id != REST_ALL:
                try:
                    address_id = int(address_id)
                except ValueError as e:
                    err_msg = 'Invalid [%s] value. %s'
                    raise ValueError(err_msg % (REST_ADDRESSID, e.message))

                requested_address = self.address_data.get_data(addr_id=address_id)
                if requested_address is None:
                    msg = 'Requested address %s for route is not registered.' % address_id
                    raise CommandFailure(msg=msg)

        if address is None:
            msg = 'Gateway=%s\'s address is not registered.' % gateway
            raise CommandFailure(msg=msg)
        elif dst_ip == address.default_gw:
            msg = 'Gateway=%s is used as default gateway of address_id=%d'\
                % (gateway, address.address_id)
            raise CommandFailure(msg=msg)
        else:
            src_ip = address.default_gw
            route = self.policy_routing_tbl.add(destination, gateway, requested_address)
            self._set_route_packetin(route)
            self.send_arp_request(src_ip, dst_ip)
            return route.route_id

    def _set_dhcp_data(self, dhcp_server_list, update_records=False):
        # OK - we've received a list of DHCP servers here.
        # We should now check to see if at least one is reachable.
        # We should, at least semi-regularly, update them.
        # This should be a rule with a short-lived idle time.
        # Actually - how about this:
        # For each IP in the list we receive:
        # - Check that it's a valid IP
        # - Check that it's one that we should be able to reach via (via a bypass or production route with a gateway).
        # - Append the IP to the list of dhcp servers for the policy routing table for this VLAN
        # - Send an ICMP ping to the IP.
        # We can then, as a periodic task, run _set_dhcp_data on the routing table,
        # which will result in a population of the switch rules, via the ICMP response handler.
        # This means that this method will need another parameter, that says whether to update the list of DHCP servers or not.
        # The ICMP response handler will check to see if the response is from one of the DHCP servers,
        # and will send an update to the switches. 

        # Check to see that IPs submitted as DHCP servers are valid.
        # FIXME: create a DHCPServer object, that has the IP address of the DHCP server, as well as whether it has been verified or not.
        err_msg = 'Invalid [%s] value.' % REST_DHCP
        for server in dhcp_server_list:
            self.logger.info('Testing DHCP IP [%s]', server)
            ip_addr_aton(server, err_msg=err_msg)

        # OK - now that we're sure all of the servers in the list have valid IP addresses, set the records.
        self.policy_routing_tbl.dhcp_servers = dhcp_server_list

        # Next, loop over the list one more time; this time, check how best to ping each server in the list,
        # then do so.
        for server in self.policy_routing_tbl.dhcp_servers:
            pass # FIXME

        # OK - this is broken, for now.
        # What I need to do: figure out *a* port, *any port* that leads to the production network,
        # or, to a network that contains the subnet of the DHCP servers (if they are locally attached), and ping out those ports.
        #production_route = self.policy_routing_tbl.get_data(dst_ip=ip_addr_aton(INADDR_ANY_BASE))
        #if production_route is not None:
        #    dst_ip = production_route.gateway_ip
        #    dst_mac = production_route.gateway_mac
        #    self.logger.info('Found production gateway at IP [%s], MAC [%s]', dst_ip, dst_mac)
        #    address = self.address_data.get_data(ip=dst_ip)
        #    src_ip = address.default_gw
        #    self.logger.info('Will use source IP [%s]', src_ip)
        #else:
        #    self.logger.info('Unable to find production gateway in tables')
        #    return

        # FIXME: we need to get the "default gateway" from the address data.
        # OK - we now know where the gateways are.
        # Now - we need to look at the DHCP server IP, and see if it is an "internal host" - meaning, it's in a subnet we know about.
        # If it is? The DHCP server *may* be directly attached to the switch.
        # That means that we need to send our probe out all of the ports on the switch, so that we can try to find the DHCP server.
        # If the DHCP server is *not* in a subnet we know about? We should try sending a probe out the known gateway ports, bound for the DHCP server.
        
        # Basically? We need to send an ICMP from the "IP of the switch" to the production gateway serving the subnet to which the switch belongs, or, if the DHCP server is in a subnet the switch knows about, flood it out...
        # And really? We don't need to be doing ICMP; we *should* be doing DHCP (and abusing the protocol slightly to do what is wanted)
        gateways = self.policy_routing_tbl.get_all_gateway_info()
        for gateway in gateways:
            gateway_ip, gateway_mac = gateway
            address = self.address_data.get_data(ip=gateway_ip)
            if gateway_mac is not None and address is not None:
                src_ip = address.default_gw
                for send_port in self.port_data.values():
                    src_mac = send_port.mac
                    out_port = send_port.port_no
                    header_list = dict()
                    header_list[ETHERNET] = ethernet.ethernet(src_mac, gateway_mac, ether.ETH_TYPE_IP)
                    for server in dhcp_server_list:
                        #header_list[IPV4] = ipv4.ipv4(src=server, dst=src_ip)
                        header_list[IPV4] = ipv4.ipv4(src=INADDR_BROADCAST_BASE, dst=INADDR_ANY_BASE)
                        #data = icmp.echo()
                        #self.ofctl.send_icmp(out_port, header_list, self.vlan_id,
                        #                     icmp.ICMP_ECHO_REQUEST,
                        #                     0,
                        #                     icmp_data=data, out_port=out_port)
                        self.ofctl.send_dhcp_discover(out_port, header_list, self.vlan_id, out_port=out_port)
            else:
                self.logger.info('Unable to find path to gateway [%s] while attempting to verify DHCP server [%s]', gateway_ip, server)

    def _set_defaultroute_drop(self):
        cookie = self._id_to_cookie(REST_VLANID, self.vlan_id)
        priority = self._get_priority(PRIORITY_DEFAULT_ROUTING)
        outport = None  # for drop
        self.ofctl.set_routing_flow(cookie, priority, outport,
                                    dl_vlan=self.vlan_id)
        self.logger.info('Set default route (drop) flow [cookie=0x%x]', cookie)

    def _set_route_packetin(self, route):
        cookie = self._id_to_cookie(REST_ROUTEID, route.route_id)
        priority, log_msg = self._get_priority(PRIORITY_TYPE_ROUTE,
                                               route=route)
        self.ofctl.set_packetin_flow(cookie, priority,
                                     dl_type=ether.ETH_TYPE_IP,
                                     dl_vlan=self.vlan_id,
                                     dst_ip=route.dst_ip,
                                     dst_mask=route.dst_netmask,
                                     src_ip=route.src_ip,
                                     src_mask=route.src_netmask)
        self.logger.info('Set %s (packet in) flow [cookie=0x%x]', log_msg, cookie)

    def delete_data(self, data, waiters):
        if REST_ROUTEID in data:
            route_id = data[REST_ROUTEID]
            msg = self._delete_routing_data(route_id, waiters)
        elif REST_ADDRESSID in data:
            address_id = data[REST_ADDRESSID]
            msg = self._delete_address_data(address_id, waiters)
        else:
            raise ValueError('Invalid parameter.')

        return self._response(msg)

    def _delete_address_data(self, address_id, waiters):
        if address_id != REST_ALL:
            try:
                address_id = int(address_id)
            except ValueError as e:
                err_msg = 'Invalid [%s] value. %s'
                raise ValueError(err_msg % (REST_ADDRESSID, e.message))

        skip_ids = self._chk_addr_relation_route(address_id)

        # Get all flow.
        delete_list = []
        msgs = self.ofctl.get_all_flow(waiters)
        max_id = UINT16_MAX
        for msg in msgs:
            for stats in msg.body:
                vlan_id = VlanRouter._cookie_to_id(REST_VLANID, stats.cookie)
                if vlan_id != self.vlan_id:
                    continue
                addr_id = VlanRouter._cookie_to_id(REST_ADDRESSID,
                                                   stats.cookie)
                if addr_id in skip_ids:
                    continue
                elif address_id == REST_ALL:
                    if addr_id <= COOKIE_DEFAULT_ID or max_id < addr_id:
                        continue
                elif address_id != addr_id:
                    continue
                delete_list.append(stats)

        delete_ids = []
        for flow_stats in delete_list:
            # Delete flow
            self.ofctl.delete_flow(flow_stats)
            address_id = VlanRouter._cookie_to_id(REST_ADDRESSID,
                                                  flow_stats.cookie)

            del_address = self.address_data.get_data(addr_id=address_id)
            if del_address is not None:
                # Clean up suspend packet threads.
                self.packet_buffer.delete(del_addr=del_address)

                # Delete data.
                self.address_data.delete(address_id)
                if address_id not in delete_ids:
                    delete_ids.append(address_id)

        msg = {}
        if delete_ids:
            delete_ids = ','.join(str(addr_id) for addr_id in delete_ids)
            details = 'Delete address [address_id=%s]' % delete_ids
            msg = {REST_RESULT: REST_OK, REST_DETAILS: details}

        if skip_ids:
            skip_ids = ','.join(str(addr_id) for addr_id in skip_ids)
            details = 'Skip delete (related route exist) [address_id=%s]'\
                % skip_ids
            if msg:
                msg[REST_DETAILS] += ', %s' % details
            else:
                msg = {REST_RESULT: REST_NG, REST_DETAILS: details}

        return msg

    def _delete_routing_data(self, route_id, waiters):
        if route_id != REST_ALL:
            try:
                route_id = int(route_id)
            except ValueError as e:
                err_msg = 'Invalid [%s] value. %s'
                raise ValueError(err_msg % (REST_ROUTEID, e.message))

        # Get all flow.
        msgs = self.ofctl.get_all_flow(waiters)

        delete_list = []
        for msg in msgs:
            for stats in msg.body:
                vlan_id = VlanRouter._cookie_to_id(REST_VLANID, stats.cookie)
                if vlan_id != self.vlan_id:
                    continue
                rt_id = VlanRouter._cookie_to_id(REST_ROUTEID, stats.cookie)
                if route_id == REST_ALL:
                    if rt_id == COOKIE_DEFAULT_ID:
                        continue
                elif route_id != rt_id:
                    continue
                delete_list.append(stats)

        # Delete flow.
        delete_ids = []
        for flow_stats in delete_list:
            self.ofctl.delete_flow(flow_stats)
            route_id = VlanRouter._cookie_to_id(REST_ROUTEID,
                                                flow_stats.cookie)
            self.policy_routing_tbl.delete(route_id)
            if route_id not in delete_ids:
                delete_ids.append(route_id)

            # case: Default route deleted. -> set flow (drop)
            route_type = get_priority_type(flow_stats.priority,
                                           vid=self.vlan_id)
            if route_type == PRIORITY_DEFAULT_ROUTING:
                self._set_defaultroute_drop()

        msg = {}
        if delete_ids:
            delete_ids = ','.join(str(route_id) for route_id in delete_ids)
            details = 'Delete route [route_id=%s]' % delete_ids
            msg = {REST_RESULT: REST_OK, REST_DETAILS: details}

        return msg

    def _chk_addr_relation_route(self, address_id):
        # Check exist of related routing data.
        relate_list = []
        gateways = self.policy_routing_tbl.get_all_gateway_info()
        for gateway in gateways:
            gateway_ip, gateway_mac = gateway
            address = self.address_data.get_data(ip=gateway_ip)
            if address is not None:
                if (address_id == REST_ALL
                        and address.address_id not in relate_list):
                    relate_list.append(address.address_id)
                elif address.address_id == address_id:
                    relate_list = [address_id]
                    break
        return relate_list

    def packet_in_handler(self, msg, header_list):
        # Check invalid TTL (for OpenFlow V1.2/1.3)
        ofproto = self.dp.ofproto
        if ofproto.OFP_VERSION == ofproto_v1_2.OFP_VERSION or \
                ofproto.OFP_VERSION == ofproto_v1_3.OFP_VERSION:
            if msg.reason == ofproto.OFPR_INVALID_TTL:
                self._packetin_invalid_ttl(msg, header_list)
                return

        # Analyze event type.
        if ARP in header_list:
            self._packetin_arp(msg, header_list)
            return

        if IPV4 in header_list:
            rt_ports = self.address_data.get_default_gw()
            if header_list[IPV4].dst in rt_ports:
                # Packet to router's port.
                if ICMP in header_list:
                    if header_list[ICMP].type == icmp.ICMP_ECHO_REQUEST:
                        self._packetin_icmp_req(msg, header_list)
                        return
                    elif header_list[ICMP].type == icmp.ICMP_ECHO_REPLY:
                        self._packetin_icmp_reply(msg, header_list)
                        return
                elif TCP in header_list or UDP in header_list:
                    self._packetin_tcp_udp(msg, header_list)
                    return
            else:
                # Packet to internal host or gateway router.
                self._packetin_to_node(msg, header_list)
                return

    def _packetin_arp(self, msg, header_list):
        src_addr = self.address_data.get_data(ip=header_list[ARP].src_ip)
        self.logger.info('Handling incoming ARP from [%s] to [%s].',
                         ip_addr_ntoa(header_list[ARP].src_ip),
                         ip_addr_ntoa(header_list[ARP].dst_ip))
        if src_addr is None:
            self.logger.info('No gateway defined for subnet; not handling ARP')
            return

        # Housekeeping tasks, associated with seeing an ARP.
        # 1) Update the routing tables.
        # 2) Learn the MAC of the host.
        # FIXME: what happens here, if someone is ARP spoofing?!?
        self._update_routing_tbls(msg, header_list)
        self._learning_host_mac(msg, header_list)

        # ARP packet handling.
        in_port = self.ofctl.get_packetin_inport(msg)
        src_ip = header_list[ARP].src_ip
        dst_ip = header_list[ARP].dst_ip
        srcip = ip_addr_ntoa(src_ip)
        dstip = ip_addr_ntoa(dst_ip)
        rt_ports = self.address_data.get_default_gw()

        if src_ip == dst_ip:
            # GARP -> ALL
            # FIXME: Is there anything that can be done to mitigate a malicious GARP?
            # Answer: check proteus before sending it - but that only works if someone isn't MAC spoofing too...
            # That said - in the MAC spoofing case - the grat ARP is *not* a problem.
            output = self.ofctl.dp.ofproto.OFPP_ALL
            self.ofctl.send_packet_out(in_port, output, msg.data)

            self.logger.info('Received GARP from [%s].', srcip)
            self.logger.info('Sending GARP (flood)')

        elif dst_ip not in rt_ports:
            dst_addr = self.address_data.get_data(ip=dst_ip)
            if (dst_addr is not None and
                    src_addr.address_id == dst_addr.address_id):
                # ARP from internal host -> ALL (in the same address range, which must be defined)
                output = self.ofctl.dp.ofproto.OFPP_ALL
                self.ofctl.send_packet_out(in_port, output, msg.data)

                self.logger.info('Received ARP from an internal host [%s].', srcip)
                self.logger.info('Sending ARP (flood)')
        else:
            if header_list[ARP].opcode == arp.ARP_REQUEST:
                # ARP request to router port -> send ARP reply
                src_mac = header_list[ARP].src_mac
                dst_mac = self.port_data[in_port].mac
                arp_target_mac = dst_mac
                output = in_port
                in_port = self.ofctl.dp.ofproto.OFPP_CONTROLLER

                self.ofctl.send_arp(arp.ARP_REPLY, self.vlan_id,
                                    dst_mac, src_mac, dst_ip, src_ip,
                                    arp_target_mac, in_port, output)

                log_msg = 'Received ARP request from [%s] to router port [%s].'
                self.logger.info(log_msg, srcip, dstip)
                self.logger.info('Send ARP reply to [%s] on port [%s]', srcip, output)

            elif header_list[ARP].opcode == arp.ARP_REPLY:
                #  ARP reply to router port -> suspend packets forward
                log_msg = 'Received ARP reply from [%s] to router port [%s].'
                self.logger.info(log_msg, srcip, dstip)

                packet_list = self.packet_buffer.get_data(src_ip)
                if packet_list:
                    # stop ARP reply wait thread.
                    for suspend_packet in packet_list:
                        self.packet_buffer.delete(pkt=suspend_packet)

                    # send suspend packet.
                    output = self.ofctl.dp.ofproto.OFPP_TABLE
                    for suspend_packet in packet_list:
                        self.ofctl.send_packet_out(suspend_packet.in_port,
                                                   output,
                                                   suspend_packet.data)
                        self.logger.info('Send suspend packet to [%s].', srcip)

    def _packetin_icmp_req(self, msg, header_list):
        # Send ICMP echo reply.
        in_port = self.ofctl.get_packetin_inport(msg)
        self.ofctl.send_icmp(in_port, header_list, self.vlan_id,
                             icmp.ICMP_ECHO_REPLY,
                             icmp.ICMP_ECHO_REPLY_CODE,
                             icmp_data=header_list[ICMP].data)

        srcip = ip_addr_ntoa(header_list[IPV4].src)
        dstip = ip_addr_ntoa(header_list[IPV4].dst)
        log_msg = 'Received ICMP echo request from [%s] to router port [%s].'
        self.logger.info(log_msg, srcip, dstip)
        self.logger.info('Send ICMP echo reply to [%s].', srcip)

    def _packetin_icmp_reply(self, msg, header_list):
        # Deal with ICMP echo reply; may be used for DHCP, etc.
        in_port = self.ofctl.get_packetin_inport(msg)

        srcip = ip_addr_ntoa(header_list[IPV4].src)
        dstip = ip_addr_ntoa(header_list[IPV4].dst)
        log_msg = 'Received ICMP echo reply from [%s] to router port [%s].'
        self.logger.info(log_msg, srcip, dstip)

    def _packetin_tcp_udp(self, msg, header_list):
        # Log the receipt of the packet...
        srcip = ip_addr_ntoa(header_list[IPV4].src)
        dstip = ip_addr_ntoa(header_list[IPV4].dst)
        self.logger.info('Received TCP/UDP from [%s] to router port [%s].', srcip, dstip)

        # ...and then send an ICMP port unreachable.
        in_port = self.ofctl.get_packetin_inport(msg)
        self.ofctl.send_icmp(in_port, header_list, self.vlan_id,
                             icmp.ICMP_DEST_UNREACH,
                             icmp.ICMP_PORT_UNREACH_CODE,
                             msg_data=msg.data)
        self.logger.info('Sent ICMP destination unreachable to [%s] in response to TCP/UDP packet to router port [%s].', srcip, dstip)

    def _packetin_to_node(self, msg, header_list):
        if len(self.packet_buffer) >= MAX_SUSPENDPACKETS:
            self.logger.info('Packet is dropped, MAX_SUSPENDPACKETS exceeded.')
            return

        # Log the receipt of the packet
        srcip = ip_addr_ntoa(header_list[IPV4].src)
        dstip = ip_addr_ntoa(header_list[IPV4].dst)
        self.logger.info('Received TCP/UDP from [%s] destined for [%s].', srcip, dstip)
        
        # Determine if this is a DHCP packet, and send it out.
        # FIXME:
        # Check to see if the packet is a DHCP OFFER.
        # Check to see if IP address if one of the configured DHCP server addresses.
        # If it is not, check that the IP address is that of the default gateway for the subnet (if such exists).
        # Presuming the correct set of the above conditions is met, RARP for the requesting MAC from the DHCP header list.
        # Upon finding the port on which the MAC lives, send out the DHCP OFFER.
        if DHCP in header_list:
            op_type = None
            flood = False
            dhcp_state = ord([opt for opt in header_list[DHCP].options.option_list if opt.tag == dhcp.DHCP_MESSAGE_TYPE_OPT][0].value)

            if dhcp_state == dhcp.DHCP_OFFER:
                op_type = "OFFER"
            elif dhcp_state == dhcp.DHCP_ACK:
                op_type = "ACK"

            if op_type is not None:
                flood = True

            if ((header_list[DHCP].op == dhcp.DHCP_BOOT_REPLY) and flood):
                self.logger.debug('Flooding received DHCP %s for MAC address [%s].', op_type, header_list[ETHERNET].dst)
                in_port = self.ofctl.get_packetin_inport(msg)
                output = self.ofctl.dp.ofproto.OFPP_ALL
                self.ofctl.send_packet_out(in_port, output, msg.data)

        # Send ARP request to get node MAC address.
        in_port = self.ofctl.get_packetin_inport(msg)
        src_ip = None
        dst_ip = header_list[IPV4].dst

        address = self.address_data.get_data(ip=dst_ip)
        if address is not None:
            log_msg = 'Received IP packet from [%s] to an internal host [%s].'
            self.logger.info(log_msg, srcip, dstip)
            src_ip = address.default_gw
        else:
            route = self.policy_routing_tbl.get_data(dst_ip=dst_ip, src_ip=srcip)
            if route is not None:
                log_msg = 'Received IP packet from [%s] to [%s].'
                self.logger.info(log_msg, srcip, dstip)
                gw_address = self.address_data.get_data(ip=route.gateway_ip)
                if gw_address is not None:
                    src_ip = gw_address.default_gw
                    dst_ip = route.gateway_ip

        if src_ip is not None:
            self.packet_buffer.add(in_port, header_list, msg.data)
            self.send_arp_request(src_ip, dst_ip, in_port=in_port)
            self.logger.info('Send ARP request (flood) on behalf of [%s] asking who-has [%s]', srcip, dstip)
        else:
            self.logger.info('Could not find a viable path to destination [%s] for source [%s]', dstip, srcip)

    def _packetin_invalid_ttl(self, msg, header_list):
        # Send ICMP TTL error.
        srcip = ip_addr_ntoa(header_list[IPV4].src)
        self.logger.info('Received invalid ttl packet from [%s].', srcip)

        in_port = self.ofctl.get_packetin_inport(msg)
        src_ip = self._get_send_port_ip(header_list)
        if src_ip is not None:
            self.ofctl.send_icmp(in_port, header_list, self.vlan_id,
                                 icmp.ICMP_TIME_EXCEEDED,
                                 icmp.ICMP_TTL_EXPIRED_CODE,
                                 msg_data=msg.data, src_ip=src_ip)
            self.logger.info('Send ICMP time exceeded to [%s].', srcip)

    def send_arp_all_gw(self):
        gateways = self.policy_routing_tbl.get_all_gateway_info()
        for gateway in gateways:
            gateway_ip, gateway_mac = gateway
            address = self.address_data.get_data(ip=gateway_ip)
            self.send_arp_request(address.default_gw, gateway_ip)

    def send_arp_request(self, src_ip, dst_ip, in_port=None):
        # Send ARP request from all ports.
        for send_port in self.port_data.values():
            if in_port is None or in_port != send_port.port_no:
                src_mac = send_port.mac
                dst_mac = mac_lib.BROADCAST_STR
                arp_target_mac = mac_lib.DONTCARE_STR
                inport = self.ofctl.dp.ofproto.OFPP_CONTROLLER
                output = send_port.port_no
                self.ofctl.send_arp(arp.ARP_REQUEST, self.vlan_id,
                                    src_mac, dst_mac, src_ip, dst_ip,
                                    arp_target_mac, inport, output)

    def send_icmp_unreach_error(self, packet_buffer):
        # Send ICMP host unreach error.
        self.logger.info('ARP reply wait timer was timed out.')

        src_ip = self._get_send_port_ip(packet_buffer.header_list)
        if src_ip is not None:
            self.ofctl.send_icmp(packet_buffer.in_port,
                                 packet_buffer.header_list,
                                 self.vlan_id,
                                 icmp.ICMP_DEST_UNREACH,
                                 icmp.ICMP_HOST_UNREACH_CODE,
                                 msg_data=packet_buffer.data,
                                 src_ip=src_ip)

            dst_ip = ip_addr_ntoa(packet_buffer.dst_ip)
            self.logger.info('Sent ICMP destination unreachable to [%s] regarding [%s].', src_ip, dst_ip)

    def _update_routing_tbls(self, msg, header_list):
        # FIXME:
        # Need to have a way to wait a certain amount of time, to ensure that there has not been a race to MAC/ARP poison the rule table.

        # Set flow: routing to gateway.
        out_port = self.ofctl.get_packetin_inport(msg)
        src_mac = header_list[ARP].src_mac
        dst_mac = self.port_data[out_port].mac
        src_ip = header_list[ARP].src_ip

        default_route = self.policy_routing_tbl.get_data(dst_ip=INADDR_ANY_BASE, src_ip=src_ip)
        gateway_flg = False
        for table in self.policy_routing_tbl.values():
            for key, value in table.items():
                if value.gateway_ip == src_ip:
                    gateway_flg = True
                    if value.gateway_mac == src_mac:
                        continue
                    table[key].gateway_mac = src_mac

                    cookie = self._id_to_cookie(REST_ROUTEID, value.route_id)
                    priority, log_msg = self._get_priority(PRIORITY_TYPE_ROUTE,
                                                           route=value)
                    # VJO - dec_ttl needs to be set to False - Cisco doesn't know what to do with it.
                    self.ofctl.set_routing_flow(cookie, priority, out_port,
                                                dl_vlan=self.vlan_id,
                                                src_mac=dst_mac,
                                                dst_mac=src_mac,
                                                nw_src=value.src_ip,
                                                src_mask=value.src_netmask,
                                                nw_dst=value.dst_ip,
                                                dst_mask=value.dst_netmask,
                                                dec_ttl=False)
                    self.logger.info('Set %s flow [cookie=0x%x]', log_msg, cookie)
                    if default_route is not None:
                        if default_route.gateway_ip == value.gateway_ip:
                            self.ofctl.set_routing_flow(cookie, priority, out_port,
                                                        dl_vlan=self.vlan_id,
                                                        nw_src=INADDR_ANY_BASE,
                                                        nw_dst=INADDR_BROADCAST_BASE,
                                                        nw_proto=in_proto.IPPROTO_UDP,
                                                        src_port=DHCP_CLIENT_PORT,
                                                        dst_port=DHCP_SERVER_PORT)
                            self.logger.info('Set DHCP egress flow...')

        return gateway_flg

    def _learning_host_mac(self, msg, header_list):
        # FIXME:
        # 1) Make sure we don't learn the MAC of the "default gateway" (as defined by the controller for the switch).
        # 2) Need to have a way to wait a certain amount of time, to ensure that there has not been a race to MAC/ARP poison the rule table.

        # Set flow: routing to internal Host.
        out_port = self.ofctl.get_packetin_inport(msg)
        src_mac = header_list[ARP].src_mac
        dst_mac = self.port_data[out_port].mac
        src_ip = header_list[ARP].src_ip

        address = self.address_data.get_data(ip=src_ip)
        if address is not None:
            cookie = self._id_to_cookie(REST_ADDRESSID, address.address_id)
            priority = self._get_priority(PRIORITY_IMPLICIT_ROUTING)
            # VJO - dec_ttl needs to be set to False - Cisco doesn't know what to do with it.
            self.ofctl.set_routing_flow(cookie, priority,
                                        out_port, dl_vlan=self.vlan_id,
                                        src_mac=dst_mac, dst_mac=src_mac,
                                        nw_dst=src_ip,
                                        idle_timeout=IDLE_TIMEOUT,
                                        dec_ttl=False)
            self.logger.info('Set implicit routing flow [cookie=0x%x]', cookie)

    def _get_send_port_ip(self, header_list):
        try:
            src_mac = header_list[ETHERNET].src
            if IPV4 in header_list:
                src_ip = header_list[IPV4].src
            else:
                src_ip = header_list[ARP].src_ip
        except KeyError:
            self.logger.debug('Received unsupported packet.')
            return None

        address = self.address_data.get_data(ip=src_ip)
        if address is not None:
            return address.default_gw
        else:
            route = self.policy_routing_tbl.get_data(gw_mac=src_mac, src_ip=src_ip)
            if route is not None:
                address = self.address_data.get_data(ip=route.gateway_ip)
                if address is not None:
                    return address.default_gw

        self.logger.debug('Received packet from unknown IP [%s].', ip_addr_ntoa(src_ip))
        return None


class PortData(dict):
    def __init__(self, ports):
        super(PortData, self).__init__()
        for port in ports.values():
            data = Port(port.port_no, port.hw_addr)
            self[port.port_no] = data


class Port(object):
    def __init__(self, port_no, hw_addr):
        super(Port, self).__init__()
        self.port_no = port_no
        self.mac = hw_addr


class AddressData(dict):
    def __init__(self):
        super(AddressData, self).__init__()
        self.address_id = 1

    def add(self, address):
        err_msg = 'Invalid [%s] value.' % REST_ADDRESS
        nw_addr, mask, default_gw = nw_addr_aton(address, err_msg=err_msg)

        # Check overlaps
        for other in self.values():
            other_mask = mask_ntob(other.netmask)
            add_mask = mask_ntob(mask, err_msg=err_msg)
            if (other.nw_addr == ipv4_apply_mask(default_gw, other.netmask) or
                    nw_addr == ipv4_apply_mask(other.default_gw, mask,
                                               err_msg)):
                msg = 'Address overlaps [address_id=%d]' % other.address_id
                raise CommandFailure(msg=msg)

        address = Address(self.address_id, nw_addr, mask, default_gw)
        ip_str = ip_addr_ntoa(nw_addr)
        key = '%s/%d' % (ip_str, mask)
        self[key] = address

        self.address_id += 1
        self.address_id &= UINT32_MAX
        if self.address_id == COOKIE_DEFAULT_ID:
            self.address_id = 1

        return address

    def delete(self, address_id):
        for key, value in self.items():
            if value.address_id == address_id:
                del self[key]
                return

    def get_default_gw(self):
        return [address.default_gw for address in self.values()]

    def get_data(self, addr_id=None, ip=None):
        for address in self.values():
            if addr_id is not None:
                if addr_id == address.address_id:
                    return address
            else:
                assert ip is not None
                if ipv4_apply_mask(ip, address.netmask) == address.nw_addr:
                    return address
        return None


class Address(object):
    def __init__(self, address_id, nw_addr, netmask, default_gw):
        super(Address, self).__init__()
        self.address_id = address_id
        self.nw_addr = nw_addr
        self.netmask = netmask
        self.default_gw = default_gw

    def __contains__(self, ip):
        return bool(ipv4_apply_mask(ip, self.netmask) == self.nw_addr)


class PolicyRoutingTable(dict):
    def __init__(self):
        super(PolicyRoutingTable, self).__init__()
        self[INADDR_ANY] = RoutingTable()
        self.route_id = 1
        self.dhcp_servers = []

    def add(self, dst_nw_addr, gateway_ip, src_address=None):
        err_msg = 'Invalid [%s] value.'
        added_route = None
        key = INADDR_ANY

        if src_address is not None:
            ip_str = ip_addr_ntoa(src_address.nw_addr)
            key = '%s/%d' % (ip_str, src_address.netmask)

        if key not in self:
            self.add_table(key, src_address)

        table = self[key]
        added_route = table.add(dst_nw_addr, gateway_ip, self.route_id)

        if added_route is not None:
            self.route_id += 1
            self.route_id &= UINT32_MAX
            if self.route_id == COOKIE_DEFAULT_ID:
                self.route_id = 1

        return added_route

    def delete(self, route_id):
        for table in self.values():
            table.delete(route_id)
        return

    def add_table(self, key, address):
        self[key] = RoutingTable(address)
        return self[key]

    def gc_subnet_tables(self):
        for key, value in self.items():
            if key != INADDR_ANY:
                if (len(value) == 0):
                    del self[key]
        return

    def get_all_gateway_info(self):
        all_gateway_info = []
        for table in self.values():
            all_gateway_info += table.get_all_gateway_info()
        return all_gateway_info

    def get_data(self, gw_mac=None, dst_ip=None, src_ip=None):
        desired_table = self[INADDR_ANY]

        if src_ip is not None:
            for table in self.values():
                if table.src_address is not None:
                     if (table.src_address.nw_addr == ipv4_apply_mask(src_ip, table.src_address.netmask)):
                         desired_table = table
                         break

        route = desired_table.get_data(gw_mac, dst_ip)

        if ((route is None) and (desired_table != self[INADDR_ANY])):
            route = self[INADDR_ANY].get_data(gw_mac, dst_ip)

        return route


class RoutingTable(dict):
    def __init__(self, address=None):
        super(RoutingTable, self).__init__()
        self.src_address = address

    def add(self, dst_nw_addr, gateway_ip, route_id):
        err_msg = 'Invalid [%s] value.'

        if dst_nw_addr == INADDR_ANY:
            dst_ip = 0
            dst_netmask = 0
        else:
            dst_ip, dst_netmask, dst_dummy = nw_addr_aton(
                dst_nw_addr, err_msg=err_msg % REST_DESTINATION)

        gateway_ip = ip_addr_aton(gateway_ip, err_msg=err_msg % REST_GATEWAY)

        dst_ip_str = ip_addr_ntoa(dst_ip)
        key = '%s/%d' % (dst_ip_str, dst_netmask)

        # Check overlaps
        overlap_route = None
        if key in self:
            overlap_route = self[key].route_id

        if overlap_route is not None:
            msg = 'Destination overlaps [route_id=%d]' % overlap_route
            raise CommandFailure(msg=msg)

        routing_data = Route(route_id, dst_ip, dst_netmask, gateway_ip, self.src_address)
        self[key] = routing_data

        return routing_data

    def delete(self, route_id):
        for key, value in self.items():
            if value.route_id == route_id:
                del self[key]
                return

    def get_all_gateway_info(self):
        all_gateway_info = []
        for route in self.values():
            gateway_info = (route.gateway_ip, route.gateway_mac)
            all_gateway_info.append(gateway_info)
        return all_gateway_info

    def get_data(self, gw_mac=None, dst_ip=None):
        if gw_mac is not None:
            for route in self.values():
                if gw_mac == route.gateway_mac:
                    return route
            return None

        elif dst_ip is not None:
            get_route = None
            mask = 0
            for route in self.values():
                if ipv4_apply_mask(dst_ip, route.dst_netmask) == route.dst_ip:
                    # For longest match
                    if mask < route.dst_netmask:
                        get_route = route
                        mask = route.dst_netmask

            if get_route is None:
                get_route = self.get(INADDR_ANY, None)
            return get_route
        else:
            return None


class Route(object):
    def __init__(self, route_id, dst_ip, dst_netmask, gateway_ip, src_address=None):
        super(Route, self).__init__()
        self.route_id = route_id
        self.dst_ip = dst_ip
        self.dst_netmask = dst_netmask
        self.gateway_ip = gateway_ip
        self.gateway_mac = None
        if src_address is None:
            self.src_ip = 0
            self.src_netmask = 0
        else:
            self.src_ip = src_address.nw_addr
            self.src_netmask = src_address.netmask


class SuspendPacketList(list):
    def __init__(self, timeout_function):
        super(SuspendPacketList, self).__init__()
        self.timeout_function = timeout_function

    def add(self, in_port, header_list, data):
        suspend_pkt = SuspendPacket(in_port, header_list, data,
                                    self.wait_arp_reply_timer)
        self.append(suspend_pkt)

    def delete(self, pkt=None, del_addr=None):
        if pkt is not None:
            del_list = [pkt]
        else:
            assert del_addr is not None
            del_list = [pkt for pkt in self if pkt.dst_ip in del_addr]

        for pkt in del_list:
            self.remove(pkt)
            hub.kill(pkt.wait_thread)
            pkt.wait_thread.wait()

    def get_data(self, dst_ip):
        return [pkt for pkt in self if pkt.dst_ip == dst_ip]

    def wait_arp_reply_timer(self, suspend_pkt):
        hub.sleep(ARP_REPLY_TIMER)
        if suspend_pkt in self:
            self.timeout_function(suspend_pkt)
            self.delete(pkt=suspend_pkt)


class SuspendPacket(object):
    def __init__(self, in_port, header_list, data, timer):
        super(SuspendPacket, self).__init__()
        self.in_port = in_port
        self.dst_ip = header_list[IPV4].dst
        self.header_list = header_list
        self.data = data
        # Start ARP reply wait timer.
        self.wait_thread = hub.spawn(timer, self)


class OfCtl(object):
    _OF_VERSIONS = {}

    @staticmethod
    def register_of_version(version):
        def _register_of_version(cls):
            OfCtl._OF_VERSIONS.setdefault(version, cls)
            return cls
        return _register_of_version

    @staticmethod
    def factory(dp, logger):
        of_version = dp.ofproto.OFP_VERSION
        if of_version in OfCtl._OF_VERSIONS:
            ofctl = OfCtl._OF_VERSIONS[of_version](dp, logger)
        else:
            raise OFPUnknownVersion(version=of_version)

        return ofctl

    def __init__(self, dp, logger):
        super(OfCtl, self).__init__()
        self.dp = dp
        self.sw_id = {'sw_id': dpid_lib.dpid_to_str(dp.id)}
        self.logger = logger

    def set_sw_config_for_ttl(self):
        # OpenFlow v1_2/1_3.
        pass

    def clear_flows(self):
        # Abstract method
        raise NotImplementedError()

    def set_flow(self, cookie, priority, dl_type=0, dl_dst=0, dl_vlan=0,
                 nw_src=0, src_mask=32, nw_dst=0, dst_mask=32,
                 nw_proto=0, idle_timeout=0, actions=None):
        # Abstract method
        raise NotImplementedError()

    def send_arp(self, arp_opcode, vlan_id, src_mac, dst_mac,
                 src_ip, dst_ip, arp_target_mac, in_port, output):
        # Generate ARP packet
        if vlan_id != VLANID_NONE:
            ether_proto = ether.ETH_TYPE_8021Q
            pcp = 0
            cfi = 0
            vlan_ether = ether.ETH_TYPE_ARP
            v = vlan.vlan(pcp, cfi, vlan_id, vlan_ether)
        else:
            ether_proto = ether.ETH_TYPE_ARP
        hwtype = 1
        arp_proto = ether.ETH_TYPE_IP
        hlen = 6
        plen = 4

        pkt = packet.Packet()
        e = ethernet.ethernet(dst_mac, src_mac, ether_proto)
        a = arp.arp(hwtype, arp_proto, hlen, plen, arp_opcode,
                    src_mac, src_ip, arp_target_mac, dst_ip)
        pkt.add_protocol(e)
        if vlan_id != VLANID_NONE:
            pkt.add_protocol(v)
        pkt.add_protocol(a)
        pkt.serialize()

        # Send packet out
        self.send_packet_out(in_port, output, pkt.data, data_str=str(pkt))

    def send_dhcp_discover(self, in_port, protocol_list, vlan_id, out_port=None):
        # Generate DHCP discover packet
        offset = ethernet.ethernet._MIN_LEN

        if vlan_id != VLANID_NONE:
            ether_proto = ether.ETH_TYPE_8021Q
            pcp = 0
            cfi = 0
            vlan_ether = ether.ETH_TYPE_IP
            v = vlan.vlan(pcp, cfi, vlan_id, vlan_ether)
            offset += vlan.vlan._MIN_LEN
        else:
            ether_proto = ether.ETH_TYPE_IP

        eth = protocol_list[ETHERNET]
        e = ethernet.ethernet(eth.src, eth.dst, ether_proto)

        ip = protocol_list[IPV4]
        src_ip = ip.dst
        i = ipv4.ipv4(dst=ip.src, src=src_ip, proto=inet.IPPROTO_UDP)

        up = udp.udp(src_port=DHCP_CLIENT_PORT, dst_port=DHCP_SERVER_PORT)

        op = dhcp.DHCP_BOOT_REQUEST
        chaddr = eth.src
        option_list = [dhcp.option(dhcp.DHCP_MESSAGE_TYPE_OPT, bytes(chr(dhcp.DHCP_REQUEST)), 1)]
        # FIXME: magic cookie should be random
        magic_cookie = '99.130.83.99'
        options = dhcp.options(option_list=option_list, magic_cookie=magic_cookie)

        dh = dhcp.dhcp(op=op, chaddr=chaddr, options=options)

        pkt = packet.Packet()
        pkt.add_protocol(e)
        if vlan_id != VLANID_NONE:
            pkt.add_protocol(v)
        pkt.add_protocol(i)
        pkt.add_protocol(up)
        pkt.add_protocol(dh)
        pkt.serialize()

        if out_port is None:
            out_port = self.dp.ofproto.OFPP_IN_PORT

        # Send packet out
        self.send_packet_out(in_port, out_port,
                             pkt.data, data_str=str(pkt))

    def send_icmp(self, in_port, protocol_list, vlan_id, icmp_type,
                  icmp_code, icmp_data=None, msg_data=None, src_ip=None, out_port=None):
        # Generate ICMP reply packet
        csum = 0
        offset = ethernet.ethernet._MIN_LEN

        if vlan_id != VLANID_NONE:
            ether_proto = ether.ETH_TYPE_8021Q
            pcp = 0
            cfi = 0
            vlan_ether = ether.ETH_TYPE_IP
            v = vlan.vlan(pcp, cfi, vlan_id, vlan_ether)
            offset += vlan.vlan._MIN_LEN
        else:
            ether_proto = ether.ETH_TYPE_IP

        eth = protocol_list[ETHERNET]
        e = ethernet.ethernet(eth.src, eth.dst, ether_proto)

        if icmp_data is None and msg_data is not None:
            ip_datagram = msg_data[offset:]
            if icmp_type == icmp.ICMP_DEST_UNREACH:
                icmp_data = icmp.dest_unreach(data_len=len(ip_datagram),
                                              data=ip_datagram)
            elif icmp_type == icmp.ICMP_TIME_EXCEEDED:
                icmp_data = icmp.TimeExceeded(data_len=len(ip_datagram),
                                              data=ip_datagram)

        ic = icmp.icmp(icmp_type, icmp_code, csum, data=icmp_data)

        ip = protocol_list[IPV4]
        if src_ip is None:
            src_ip = ip.dst
        ip_total_length = ip.header_length * 4 + ic._MIN_LEN
        if ic.data is not None:
            ip_total_length += ic.data._MIN_LEN
            if ic.data.data is not None:
                ip_total_length += len(ic.data.data)
        i = ipv4.ipv4(ip.version, ip.header_length, ip.tos,
                      ip_total_length, ip.identification, ip.flags,
                      ip.offset, DEFAULT_TTL, inet.IPPROTO_ICMP, csum,
                      src_ip, ip.src)

        pkt = packet.Packet()
        pkt.add_protocol(e)
        if vlan_id != VLANID_NONE:
            pkt.add_protocol(v)
        pkt.add_protocol(i)
        pkt.add_protocol(ic)
        pkt.serialize()

        if out_port is None:
            out_port = self.dp.ofproto.OFPP_IN_PORT

        # Send packet out
        self.send_packet_out(in_port, out_port,
                             pkt.data, data_str=str(pkt))

    def send_packet_out(self, in_port, output, data, data_str=None):
        actions = [self.dp.ofproto_parser.OFPActionOutput(output, 0)]
        self.dp.send_packet_out(buffer_id=UINT32_MAX, in_port=in_port,
                                actions=actions, data=data)
        #TODO: Packet library convert to string
        #if data_str is None:
        #    data_str = str(packet.Packet(data))
        #self.logger.debug('Packet out = %s', data_str)

    def set_packetin_flow(self, cookie, priority, dl_type=0, dl_dst=0,
                          dl_vlan=0, dst_ip=0, dst_mask=32, src_ip=0, src_mask=32, nw_proto=0):
        miss_send_len = UINT16_MAX
        actions = [self.dp.ofproto_parser.OFPActionOutput(
            self.dp.ofproto.OFPP_CONTROLLER, miss_send_len)]
        self.set_flow(cookie, priority, dl_type=dl_type, dl_dst=dl_dst,
                      dl_vlan=dl_vlan, nw_dst=dst_ip, dst_mask=dst_mask,
                      nw_src=src_ip, src_mask=src_mask, nw_proto=nw_proto, actions=actions)

    def send_stats_request(self, stats, waiters):
        self.dp.set_xid(stats)
        waiters_per_dp = waiters.setdefault(self.dp.id, {})
        event = hub.Event()
        msgs = []
        waiters_per_dp[stats.xid] = (event, msgs)
        self.dp.send_msg(stats)

        try:
            event.wait(timeout=OFP_REPLY_TIMER)
        except hub.Timeout:
            del waiters_per_dp[stats.xid]

        return msgs


@OfCtl.register_of_version(ofproto_v1_0.OFP_VERSION)
class OfCtl_v1_0(OfCtl):

    def __init__(self, dp, logger):
        super(OfCtl_v1_0, self).__init__(dp, logger)

    def clear_flows(self):
        ofp = self.dp.ofproto
        ofp_parser = self.dp.ofproto_parser
        mod = ofp_parser.OFPFlowMod(
            datapath=self.dp,
            match=ofp_parser.OFPMatch(),
            cookie=0,
            command=ofp.OFPFC_DELETE, 
            priority=ofp.OFP_DEFAULT_PRIORITY,
            actions=[])
        self.dp.send_msg(mod)

    def get_packetin_inport(self, msg):
        return msg.in_port

    def get_all_flow(self, waiters):
        ofp = self.dp.ofproto
        ofp_parser = self.dp.ofproto_parser

        match = ofp_parser.OFPMatch(ofp.OFPFW_ALL, 0, 0, 0,
                                    0, 0, 0, 0, 0, 0, 0, 0, 0)
        stats = ofp_parser.OFPFlowStatsRequest(self.dp, 0, match,
                                               0xff, ofp.OFPP_NONE)
        return self.send_stats_request(stats, waiters)

    def set_flow(self, cookie, priority, dl_type=0, dl_dst=0, dl_vlan=0,
                 nw_src=0, src_mask=32, nw_dst=0, dst_mask=32,
                 src_port=0, dst_port=0,
                 nw_proto=0, idle_timeout=0, actions=None):
        ofp = self.dp.ofproto
        ofp_parser = self.dp.ofproto_parser
        cmd = ofp.OFPFC_ADD

        # Match
        wildcards = ofp.OFPFW_ALL
        if dl_type:
            wildcards &= ~ofp.OFPFW_DL_TYPE
        if dl_dst:
            wildcards &= ~ofp.OFPFW_DL_DST
        if dl_vlan:
            wildcards &= ~ofp.OFPFW_DL_VLAN
        if nw_src:
            v = (32 - src_mask) << ofp.OFPFW_NW_SRC_SHIFT | \
                ~ofp.OFPFW_NW_SRC_MASK
            wildcards &= v
            nw_src = ipv4_text_to_int(nw_src)
        if nw_dst:
            v = (32 - dst_mask) << ofp.OFPFW_NW_DST_SHIFT | \
                ~ofp.OFPFW_NW_DST_MASK
            wildcards &= v
            nw_dst = ipv4_text_to_int(nw_dst)
        if src_port:
            wildcards &= ~ofp.OFPFW_TP_SRC
        if dst_port:
            wildcards &= ~ofp.OFPFW_TP_DST
        if nw_proto:
            wildcards &= ~ofp.OFPFW_NW_PROTO

        match = ofp_parser.OFPMatch(wildcards, 0, 0, dl_dst, dl_vlan, 0,
                                    dl_type, 0, nw_proto,
                                    nw_src, nw_dst, src_port, dst_port)
        actions = actions or []

        m = ofp_parser.OFPFlowMod(self.dp, match, cookie, cmd,
                                  idle_timeout=idle_timeout,
                                  priority=priority, actions=actions)
        self.dp.send_msg(m)

    def set_routing_flow(self, cookie, priority, outport, dl_vlan=0,
                         nw_src=0, src_mask=32, nw_dst=0, dst_mask=32,
                         src_port=0, dst_port=0, src_mac=0, dst_mac=0,
                         nw_proto=0, idle_timeout=0, **dummy):
        ofp_parser = self.dp.ofproto_parser

        dl_type = ether.ETH_TYPE_IP

        # Decrement TTL value is not supported at OpenFlow V1.0
        actions = []
        if src_mac:
            actions.append(ofp_parser.OFPActionSetDlSrc(
                           mac_lib.haddr_to_bin(src_mac)))
        if dst_mac:
            actions.append(ofp_parser.OFPActionSetDlDst(
                           mac_lib.haddr_to_bin(dst_mac)))
        if outport is not None:
            actions.append(ofp_parser.OFPActionOutput(outport))

        self.set_flow(cookie, priority, dl_type=dl_type, dl_vlan=dl_vlan,
                      nw_src=nw_src, src_mask=src_mask,
                      nw_dst=nw_dst, dst_mask=dst_mask,
                      src_port=src_port, dst_port=dst_port,
                      nw_proto=nw_proto,
                      idle_timeout=idle_timeout, actions=actions)

    def delete_flow(self, flow_stats):
        match = flow_stats.match
        cookie = flow_stats.cookie
        cmd = self.dp.ofproto.OFPFC_DELETE_STRICT
        priority = flow_stats.priority
        actions = []

        flow_mod = self.dp.ofproto_parser.OFPFlowMod(
            self.dp, match, cookie, cmd, priority=priority, actions=actions)
        self.dp.send_msg(flow_mod)
        self.logger.info('Delete flow [cookie=0x%x]', cookie)


class OfCtl_after_v1_2(OfCtl):

    def __init__(self, dp, logger):
        super(OfCtl_after_v1_2, self).__init__(dp, logger)

    def set_sw_config_for_ttl(self):
        pass

    def clear_flows(self):
        ofp = self.dp.ofproto
        ofp_parser = self.dp.ofproto_parser
        mod = ofp_parser.OFPFlowMod(self.dp, 0, 0, ofp.OFPTT_ALL,
                                    ofp.OFPFC_DELETE, 0, 0, 1, ofp.OFPCML_NO_BUFFER,
                                    ofp.OFPP_ANY, ofp.OFPG_ANY, 0, ofp_parser.OFPMatch(), [])
        self.dp.send_msg(mod)

    def get_packetin_inport(self, msg):
        in_port = self.dp.ofproto.OFPP_ANY
        for match_field in msg.match.fields:
            if match_field.header == self.dp.ofproto.OXM_OF_IN_PORT:
                in_port = match_field.value
                break
        return in_port

    def get_all_flow(self, waiters):
        pass

    def set_flow(self, cookie, priority, dl_type=0, dl_dst=0, dl_vlan=0,
                 nw_src=0, src_mask=32, nw_dst=0, dst_mask=32,
                 src_port=0, dst_port=0,
                 nw_proto=0, idle_timeout=0, actions=None):
        ofp = self.dp.ofproto
        ofp_parser = self.dp.ofproto_parser
        cmd = ofp.OFPFC_ADD

        table_id = 0 # The default is table 0

        # Match
        match = ofp_parser.OFPMatch()
        if dl_type:
            match.set_dl_type(dl_type)
            if dl_type == ether.ETH_TYPE_IP:
                table_id = 1
        if dl_dst:
            match.set_dl_dst(dl_dst)
        if dl_vlan:
            match.set_vlan_vid(dl_vlan)
        if nw_src:
            match.set_ipv4_src_masked(ipv4_text_to_int(nw_src),
                                      mask_ntob(src_mask))
            table_id = 1
        if nw_dst:
            match.set_ipv4_dst_masked(ipv4_text_to_int(nw_dst),
                                      mask_ntob(dst_mask))
            table_id = 1

        if nw_proto:
            if dl_type == ether.ETH_TYPE_IP:
                match.set_ip_proto(nw_proto)
                table_id = 1
                if src_port:
                    if nw_proto == in_proto.IPPROTO_TCP:
                        match.set_tcp_src(src_port)
                    elif nw_proto == in_proto.IPPROTO_UDP:
                        match.set_udp_src(src_port)
                if dst_port:
                    if nw_proto == in_proto.IPPROTO_TCP:
                        match.set_tcp_dst(dst_port)
                    elif nw_proto == in_proto.IPPROTO_UDP:
                        match.set_udp_dst(dst_port)
            elif dl_type == ether.ETH_TYPE_ARP:
                match.set_arp_opcode(nw_proto)

        # FIXME: We're working around the fact that our Aristas have 1 hardware table, and our
        # Ciscos have 2, in OF 1.3 mode.
        # Right now, we check the number of tables we matched to the datapath.
        # What *should* we be doing? Checking table features, and being more clever.
        if self.dp.n_tables == 1:
            table_id = 0

        # Instructions
        actions = actions or []
        inst = [ofp_parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS,
                                                 actions)]

        m = ofp_parser.OFPFlowMod(self.dp, cookie, 0, table_id, cmd, idle_timeout,
                                  0, priority, UINT32_MAX, ofp.OFPP_ANY,
                                  ofp.OFPG_ANY, 0, match, inst)
        self.dp.send_msg(m)

    def set_routing_flow(self, cookie, priority, outport, dl_vlan=0,
                         nw_src=0, src_mask=32, nw_dst=0, dst_mask=32,
                         src_port=0, dst_port=0, src_mac=0, dst_mac=0,
                         nw_proto=0, idle_timeout=0, dec_ttl=False):
        ofp = self.dp.ofproto
        ofp_parser = self.dp.ofproto_parser

        dl_type = ether.ETH_TYPE_IP

        actions = []
        if dec_ttl:
            actions.append(ofp_parser.OFPActionDecNwTtl())
        if src_mac:
            actions.append(ofp_parser.OFPActionSetField(eth_src=src_mac))
        if dst_mac:
            actions.append(ofp_parser.OFPActionSetField(eth_dst=dst_mac))
        if outport is not None:
            actions.append(ofp_parser.OFPActionOutput(outport, 0))

        self.set_flow(cookie, priority, dl_type=dl_type, dl_vlan=dl_vlan,
                      nw_src=nw_src, src_mask=src_mask,
                      nw_dst=nw_dst, dst_mask=dst_mask,
                      src_port=src_port, dst_port=dst_port,
                      nw_proto=nw_proto,
                      idle_timeout=idle_timeout, actions=actions)

    def delete_flow(self, flow_stats):
        ofp = self.dp.ofproto
        ofp_parser = self.dp.ofproto_parser

        cmd = ofp.OFPFC_DELETE
        cookie = flow_stats.cookie
        cookie_mask = UINT64_MAX
        match = ofp_parser.OFPMatch()
        inst = []

        flow_mod = ofp_parser.OFPFlowMod(self.dp, cookie, cookie_mask, ofp.OFPTT_ALL, cmd,
                                         0, 0, 0, UINT32_MAX, ofp.OFPP_ANY,
                                         ofp.OFPG_ANY, 0, match, inst)
        self.dp.send_msg(flow_mod)
        self.logger.info('Delete flow [cookie=0x%x]', cookie)


@OfCtl.register_of_version(ofproto_v1_2.OFP_VERSION)
class OfCtl_v1_2(OfCtl_after_v1_2):

    def __init__(self, dp, logger):
        super(OfCtl_v1_2, self).__init__(dp, logger)

    def set_sw_config_for_ttl(self):
        flags = self.dp.ofproto.OFPC_INVALID_TTL_TO_CONTROLLER
        miss_send_len = UINT16_MAX
        m = self.dp.ofproto_parser.OFPSetConfig(self.dp, flags,
                                                miss_send_len)
        self.dp.send_msg(m)
        self.logger.info('Set SW config for TTL error packet in.')

    def get_all_flow(self, waiters):
        ofp = self.dp.ofproto
        ofp_parser = self.dp.ofproto_parser

        match = ofp_parser.OFPMatch()
        stats = ofp_parser.OFPFlowStatsRequest(self.dp, ofp.OFPTT_ALL, ofp.OFPP_ANY,
                                               ofp.OFPG_ANY, 0, 0, match)
        return self.send_stats_request(stats, waiters)


@OfCtl.register_of_version(ofproto_v1_3.OFP_VERSION)
class OfCtl_v1_3(OfCtl_after_v1_2):

    def __init__(self, dp, logger):
        super(OfCtl_v1_3, self).__init__(dp, logger)

    def set_sw_config_for_ttl(self):
        packet_in_mask = (1 << self.dp.ofproto.OFPR_ACTION |
                          1 << self.dp.ofproto.OFPR_INVALID_TTL)
        port_status_mask = (1 << self.dp.ofproto.OFPPR_ADD |
                            1 << self.dp.ofproto.OFPPR_DELETE |
                            1 << self.dp.ofproto.OFPPR_MODIFY)
        flow_removed_mask = (1 << self.dp.ofproto.OFPRR_IDLE_TIMEOUT |
                             1 << self.dp.ofproto.OFPRR_HARD_TIMEOUT |
                             1 << self.dp.ofproto.OFPRR_DELETE)
        m = self.dp.ofproto_parser.OFPSetAsync(
            self.dp, [packet_in_mask, 0], [port_status_mask, 0],
            [flow_removed_mask, 0])
        self.dp.send_msg(m)
        self.logger.info('Set SW config for TTL error packet in.')

    def get_all_flow(self, waiters):
        ofp = self.dp.ofproto
        ofp_parser = self.dp.ofproto_parser

        match = ofp_parser.OFPMatch()
        stats = ofp_parser.OFPFlowStatsRequest(self.dp, 0, ofp.OFPTT_ALL, ofp.OFPP_ANY,
                                               ofp.OFPG_ANY, 0, 0, match)
        return self.send_stats_request(stats, waiters)


def ip_addr_aton(ip_str, err_msg=None):
    try:
        return addrconv.ipv4.bin_to_text(socket.inet_aton(ip_str))
    except (struct.error, socket.error) as e:
        if err_msg is not None:
            e.message = '%s %s' % (err_msg, e.message)
        raise ValueError(e.message)


def ip_addr_ntoa(ip):
    return socket.inet_ntoa(addrconv.ipv4.text_to_bin(ip))


def mask_ntob(mask, err_msg=None):
    try:
        return (UINT32_MAX << (32 - mask)) & UINT32_MAX
    except ValueError:
        msg = 'illegal netmask'
        if err_msg is not None:
            msg = '%s %s' % (err_msg, msg)
        raise ValueError(msg)


def ipv4_apply_mask(address, prefix_len, err_msg=None):
    import itertools

    assert isinstance(address, str)
    address_int = ipv4_text_to_int(address)
    return ipv4_int_to_text(address_int & mask_ntob(prefix_len, err_msg))


def ipv4_int_to_text(ip_int):
    assert isinstance(ip_int, (int, long))
    return addrconv.ipv4.bin_to_text(struct.pack('!I', ip_int))


def ipv4_text_to_int(ip_text):
    if ip_text == 0:
        return ip_text
    assert isinstance(ip_text, str)
    return struct.unpack('!I', addrconv.ipv4.text_to_bin(ip_text))[0]


def nw_addr_aton(nw_addr, err_msg=None):
    ip_mask = nw_addr.split('/')
    default_route = ip_addr_aton(ip_mask[0], err_msg=err_msg)
    netmask = 32
    if len(ip_mask) == 2:
        try:
            netmask = int(ip_mask[1])
        except ValueError as e:
            if err_msg is not None:
                e.message = '%s %s' % (err_msg, e.message)
            raise ValueError(e.message)
    if netmask < 0:
        msg = 'illegal netmask'
        if err_msg is not None:
            msg = '%s %s' % (err_msg, msg)
        raise ValueError(msg)
    nw_addr = ipv4_apply_mask(default_route, netmask, err_msg)
    return nw_addr, netmask, default_route
