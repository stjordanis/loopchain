# Copyright 2018 ICON Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import asyncio
import json
import leveldb
import logging
import pickle
import signal
import time
import traceback

from earlgrey import MessageQueueService

import loopchain.utils as util
from loopchain import configure as conf
from loopchain.baseservice import BroadcastScheduler, BroadcastCommand, ObjectManager, CommonSubprocess
from loopchain.baseservice import RestStubManager, NodeSubscriber
from loopchain.baseservice import StubManager, PeerManager, PeerStatus, TimerService
from loopchain.blockchain import Block, BlockBuilder, TransactionSerializer
from loopchain.channel.channel_inner_service import ChannelInnerService
from loopchain.channel.channel_property import ChannelProperty
from loopchain.channel.channel_statemachine import ChannelStateMachine
from loopchain.crypto.signature import Signer
from loopchain.peer import BlockManager
from loopchain.protos import loopchain_pb2_grpc, message_code, loopchain_pb2
from loopchain.utils import loggers, command_arguments
from loopchain.utils.icon_service import convert_params, ParamType, response_to_json_query
from loopchain.utils.message_queue import StubCollection


class ChannelService:
    def __init__(self, channel_name, amqp_target, amqp_key):
        self.__block_manager: BlockManager = None
        self.__score_container: CommonSubprocess = None
        self.__score_info: dict = None
        self.__peer_auth: Signer = None
        self.__peer_manager: PeerManager = None
        self.__broadcast_scheduler: BroadcastScheduler = None
        self.__radio_station_stub = None
        self.__consensus = None
        # self.__proposer: Proposer = None
        # self.__acceptor: Acceptor = None
        self.__timer_service = TimerService()
        self.__node_subscriber: NodeSubscriber = None

        loggers.get_preset().channel_name = channel_name
        loggers.get_preset().update_logger()

        channel_queue_name = conf.CHANNEL_QUEUE_NAME_FORMAT.format(channel_name=channel_name, amqp_key=amqp_key)
        self.__inner_service = ChannelInnerService(
            amqp_target, channel_queue_name, conf.AMQP_USERNAME, conf.AMQP_PASSWORD, channel_service=self)

        logging.info(f"ChannelService : {channel_name}, Queue : {channel_queue_name}")

        ChannelProperty().name = channel_name
        ChannelProperty().amqp_target = amqp_target

        StubCollection().amqp_key = amqp_key
        StubCollection().amqp_target = amqp_target

        command_arguments.add_raw_command(command_arguments.Type.Channel, channel_name)
        command_arguments.add_raw_command(command_arguments.Type.AMQPTarget, amqp_target)
        command_arguments.add_raw_command(command_arguments.Type.AMQPKey, amqp_key)

        ObjectManager().channel_service = self
        self.__state_machine = ChannelStateMachine(self)

    @property
    def block_manager(self):
        return self.__block_manager

    @property
    def score_container(self):
        return self.__score_container

    @property
    def score_info(self):
        return self.__score_info

    @property
    def radio_station_stub(self):
        return self.__radio_station_stub

    @property
    def peer_auth(self):
        return self.__peer_auth

    @property
    def peer_manager(self):
        return self.__peer_manager

    @property
    def broadcast_scheduler(self):
        return self.__broadcast_scheduler

    @property
    def consensus(self):
        return self.__consensus

    @property
    def acceptor(self):
        return self.__acceptor

    @property
    def timer_service(self):
        return self.__timer_service

    @property
    def state_machine(self):
        return self.__state_machine

    @property
    def inner_service(self):
        return self.__inner_service

    def serve(self):
        async def _serve():
            await StubCollection().create_peer_stub()
            results = await StubCollection().peer_stub.async_task().get_channel_info_detail(ChannelProperty().name)

            await self.init(*results)

            self.__timer_service.start()
            self.__state_machine.complete_init_components()
            logging.info(f'channel_service: init complete channel: {ChannelProperty().name}, '
                         f'state({self.__state_machine.state})')

        loop = MessageQueueService.loop
        loop.create_task(_serve())
        loop.add_signal_handler(signal.SIGINT, self.close)
        loop.add_signal_handler(signal.SIGTERM, self.close)

        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

            self.cleanup()

    def close(self):
        MessageQueueService.loop.stop()

    def cleanup(self):
        logging.info("Cleanup Channel Resources.")

        if self.__block_manager:
            self.__block_manager.stop()
            self.__block_manager = None
            logging.info("Cleanup BlockManager.")

        if self.__score_container:
            self.__score_container.stop()
            self.__score_container.wait()
            self.__score_container = None
            logging.info("Cleanup ScoreContainer.")

        if self.__broadcast_scheduler:
            self.__broadcast_scheduler.stop()
            self.__broadcast_scheduler.wait()
            self.__broadcast_scheduler = None
            logging.info("Cleanup BroadcastScheduler.")

        if self.__consensus:
            self.__consensus.stop()
            self.__consensus.wait()
            logging.info("Cleanup Consensus.")

        if self.__timer_service.is_run():
            self.__timer_service.stop()
            self.__timer_service.wait()
            logging.info("Cleanup TimerService.")

    async def init(self, peer_port, peer_target, rest_target, radio_station_target, peer_id, group_id, node_type, score_package):
        loggers.get_preset().peer_id = peer_id
        loggers.get_preset().update_logger()

        ChannelProperty().peer_port = peer_port
        ChannelProperty().peer_target = peer_target
        ChannelProperty().rest_target = rest_target
        ChannelProperty().radio_station_target = radio_station_target
        ChannelProperty().peer_id = peer_id
        ChannelProperty().group_id = group_id
        ChannelProperty().node_type = conf.NodeType(node_type)
        ChannelProperty().score_package = score_package

        self.__peer_manager = PeerManager(ChannelProperty().name)
        self.__init_peer_auth()
        self.__init_broadcast_scheduler()
        self.__init_block_manager()
        self.__init_radio_station_stub()

        await self.__init_score_container()
        await self.__inner_service.connect(conf.AMQP_CONNECTION_ATTEMPS, conf.AMQP_RETRY_DELAY, exclusive=True)

        # if conf.CONSENSUS_ALGORITHM == conf.ConsensusAlgorithm.lft:
        #     util.logger.spam(f"init consensus !")
        #     # load consensus
        #     self.__init_consensus()
        #     # load proposer
        #     self.__init_proposer(peer_id=peer_id)
        #     # load acceptor
        #     self.__init_acceptor(peer_id=peer_id)
            
        if self.is_support_node_function(conf.NodeFunction.Vote):
            if conf.ENABLE_REP_RADIO_STATION:
                self.connect_to_radio_station()
            else:
                await self.__load_peers_from_file()
                # subscribe to other peers
                self.__subscribe_to_peer_list()
                # broadcast AnnounceNewPeer to other peers
                # If allow broadcast AnnounceNewPeer here, complained peer can be leader again.
        else:
            self.__init_node_subscriber()

        self.block_manager.init_epoch()

    async def evaluate_network(self):
        await self.set_peer_type_in_channel()
        if self.block_manager.peer_type == loopchain_pb2.BLOCK_GENERATOR:
            self.__state_machine.subscribe_network()
        else:
            self.__state_machine.block_sync()

    async def subscribe_network(self):
        # Subscribe to radiostation and block_sync_target_stub
        if self.is_support_node_function(conf.NodeFunction.Vote):
            if conf.ENABLE_REP_RADIO_STATION:
                await self.subscribe_to_radio_station()

            if self.block_manager.peer_type == loopchain_pb2.PEER:
                await self.__subscribe_call_to_stub(
                    peer_stub=self.block_manager.subscribe_target_peer_stub,
                    peer_type=loopchain_pb2.PEER
                )
        else:
            await self.subscribe_to_radio_station()

        self.generate_genesis_block()

        if conf.CONSENSUS_ALGORITHM == conf.ConsensusAlgorithm.lft:
            if not self.__consensus.is_run():
                self.__consensus.change_epoch(precommit_block=self.__block_manager.get_blockchain().last_block)
                self.__consensus.start()
        elif conf.ALLOW_MAKE_EMPTY_BLOCK:
            if not self.block_manager.block_generation_scheduler.is_run():
                self.block_manager.block_generation_scheduler.start()

        self.__state_machine.complete_sync()

    def __init_peer_auth(self):
        try:
            self.__peer_auth = Signer.from_channel(ChannelProperty().name)
        except Exception as e:
            logging.exception(f"peer auth init fail cause : {e}")
            util.exit_and_msg(f"peer auth init fail cause : {e}")

    def __init_block_manager(self):
        logging.debug(f"__load_block_manager_each channel({ChannelProperty().name})")
        try:
            self.__block_manager = BlockManager(
                name="loopchain.peer.BlockManager",
                channel_manager=self,
                peer_id=ChannelProperty().peer_id,
                channel_name=ChannelProperty().name,
                level_db_identity=ChannelProperty().peer_target
            )
        except leveldb.LevelDBError as e:
            util.exit_and_msg("LevelDBError(" + str(e) + ")")

    # def __init_consensus(self):
    #     consensus = Consensus(self, ChannelProperty().name)
    #     self.__consensus = consensus
    #     self.__block_manager.consensus = consensus
    #     consensus.register_subscriber(self.__block_manager)
    #
    # def __init_proposer(self, peer_id: str):
    #     proposer = Proposer(
    #         name="loopchain.consensus.Proposer",
    #         peer_id=peer_id,
    #         channel=ChannelProperty().name,
    #         channel_service=self
    #     )
    #     self.__consensus.register_subscriber(proposer)
    #     self.__proposer = proposer
    #
    # def __init_acceptor(self, peer_id: str):
    #     acceptor = Acceptor(
    #         name="loopchain.consensus.Acceptor",
    #         consensus=self.__consensus,
    #         peer_id=peer_id,
    #         channel=ChannelProperty().name,
    #         channel_service=self
    #     )
    #     self.__consensus.register_subscriber(acceptor)
    #     self.__acceptor = acceptor

    def __init_broadcast_scheduler(self):
        scheduler = BroadcastScheduler(channel=ChannelProperty().name, self_target=ChannelProperty().peer_target)
        scheduler.start()

        self.__broadcast_scheduler = scheduler

        future = scheduler.schedule_job(BroadcastCommand.SUBSCRIBE, ChannelProperty().peer_target)
        future.result(conf.TIMEOUT_FOR_FUTURE)

    def __init_radio_station_stub(self):
        if self.is_support_node_function(conf.NodeFunction.Vote):
            if conf.ENABLE_REP_RADIO_STATION:
                self.__radio_station_stub = StubManager.get_stub_manager_to_server(
                    ChannelProperty().radio_station_target,
                    loopchain_pb2_grpc.RadioStationStub,
                    conf.CONNECTION_RETRY_TIMEOUT_TO_RS,
                    ssl_auth_type=conf.GRPC_SSL_TYPE)
        else:
            self.__radio_station_stub = RestStubManager(ChannelProperty().radio_station_target, ChannelProperty().name)

    async def __init_score_container(self):
        """create score container and save score_info and score_stub
        """
        for i in range(conf.SCORE_LOAD_RETRY_TIMES):
            try:
                self.__score_info = await self.__run_score_container()
            except BaseException as e:
                util.logger.spam(f"channel_manager:load_score_container_each score_info load fail retry({i})")
                logging.error(e)
                traceback.print_exc()
                time.sleep(conf.SCORE_LOAD_RETRY_INTERVAL)  # This blocking main thread is intended.

            else:
                break

    def __init_node_subscriber(self):
        self.__node_subscriber = NodeSubscriber(
            channel=ChannelProperty().name,
            rs_target=ChannelProperty().radio_station_target
        )

    async def __run_score_container(self):
        if not conf.USE_EXTERNAL_SCORE or conf.EXTERNAL_SCORE_RUN_IN_LAUNCHER:
            process_args = ['python3', '-m', 'loopchain', 'score',
                            '--channel', ChannelProperty().name,
                            '--score_package', ChannelProperty().score_package]
            process_args += command_arguments.get_raw_commands_by_filter(
                command_arguments.Type.AMQPTarget,
                command_arguments.Type.AMQPKey,
                command_arguments.Type.Develop,
                command_arguments.Type.ConfigurationFilePath,
                command_arguments.Type.RadioStationTarget
            )
            self.__score_container = CommonSubprocess(process_args)

        await StubCollection().create_icon_score_stub(ChannelProperty().name)
        await StubCollection().icon_score_stubs[ChannelProperty().name].connect()
        await StubCollection().icon_score_stubs[ChannelProperty().name].async_task().hello()
        return None

    async def __load_score(self):
        channel_name = ChannelProperty().name
        score_package_name = ChannelProperty().score_package

        util.logger.spam(f"peer_service:__load_score --init--")
        logging.info("LOAD SCORE AND CONNECT TO SCORE SERVICE!")

        params = dict()
        params[message_code.MetaParams.ScoreLoad.repository_path] = conf.DEFAULT_SCORE_REPOSITORY_PATH
        params[message_code.MetaParams.ScoreLoad.score_package] = score_package_name
        params[message_code.MetaParams.ScoreLoad.base] = conf.DEFAULT_SCORE_BASE
        params[message_code.MetaParams.ScoreLoad.peer_id] = ChannelProperty().peer_id
        meta = json.dumps(params)
        logging.debug(f"load score params : {meta}")

        util.logger.spam(f"peer_service:__load_score --1--")
        score_stub = StubCollection().score_stubs[channel_name]
        response = await score_stub.async_task().score_load(meta)

        logging.debug("try score load on score service: " + str(response))
        if not response:
            return None

        if response.code != message_code.Response.success:
            util.exit_and_msg("Fail Get Score from Score Server...")
            return None

        logging.debug("Get Score from Score Server...")
        score_info = json.loads(response.meta)

        logging.info("LOAD SCORE DONE!")
        util.logger.spam(f"peer_service:__load_score --end--")

        return score_info

    async def __load_peers_from_file(self):
        channel_info = await StubCollection().peer_stub.async_task().get_channel_infos()
        for peer_info in channel_info[ChannelProperty().name]["peers"]:
            self.__peer_manager.add_peer(peer_info)
            self.__broadcast_scheduler.schedule_job(BroadcastCommand.SUBSCRIBE, peer_info["peer_target"])
        self.show_peers()

    def is_support_node_function(self, node_function):
        return conf.NodeType.is_support_node_function(node_function, ChannelProperty().node_type)

    def get_channel_option(self) -> dict:
        channel_option = conf.CHANNEL_OPTION
        return channel_option[ChannelProperty().name]

    def generate_genesis_block(self):
        if self.block_manager.peer_type != loopchain_pb2.BLOCK_GENERATOR:
            return

        block_chain = self.block_manager.get_blockchain()
        if block_chain.block_height > -1:
            logging.debug("genesis block was already generated")
            return

        block_chain.generate_genesis_block()

    def connect_to_radio_station(self, is_reconnect=False):
        response = self.__radio_station_stub.call_in_times(
            method_name="ConnectPeer",
            message=loopchain_pb2.ConnectPeerRequest(
                channel=ChannelProperty().name,
                peer_object=b'',
                peer_id=ChannelProperty().peer_id,
                peer_target=ChannelProperty().peer_target,
                group_id=ChannelProperty().group_id),
            retry_times=conf.CONNECTION_RETRY_TIMES_TO_RS,
            is_stub_reuse=True,
            timeout=conf.CONNECTION_TIMEOUT_TO_RS)

        # start next ConnectPeer timer
        self.__timer_service.add_timer_convenient(timer_key=TimerService.TIMER_KEY_CONNECT_PEER,
                                                  duration=conf.CONNECTION_RETRY_TIMER,
                                                  callback=self.connect_to_radio_station,
                                                  callback_kwargs={"is_reconnect": True})

        if is_reconnect:
            return

        if response and response.status == message_code.Response.success:
            peer_list_data = pickle.loads(response.peer_list)
            self.__peer_manager.load(peer_list_data, False)
            peers, peer_list = self.__peer_manager.get_peers_for_debug()
            logging.debug("peer list update: " + peers)

            # add connected peer to processes audience
            for each_peer in peer_list:
                util.logger.spam(f"peer_service:connect_to_radio_station peer({each_peer.target}-{each_peer.status})")
                if each_peer.status == PeerStatus.connected:
                    self.__broadcast_scheduler.schedule_job(BroadcastCommand.SUBSCRIBE, each_peer.target)

    def __subscribe_to_peer_list(self):
        peer_object = self.peer_manager.get_peer(ChannelProperty().peer_id)
        peer_request = loopchain_pb2.PeerRequest(
            channel=ChannelProperty().name,
            peer_target=ChannelProperty().peer_target,
            peer_id=ChannelProperty().peer_id, group_id=ChannelProperty().group_id,
            node_type=ChannelProperty().node_type,
            peer_order=peer_object.order
        )
        self.__broadcast_scheduler.schedule_broadcast("Subscribe", peer_request)

    async def subscribe_to_radio_station(self):
        await self.__subscribe_call_to_stub(self.__radio_station_stub, loopchain_pb2.PEER)

    async def subscribe_to_peer(self, peer_id, peer_type):
        peer = self.peer_manager.get_peer(peer_id)
        peer_stub = self.peer_manager.get_peer_stub_manager(peer)

        await self.__subscribe_call_to_stub(peer_stub, peer_type)
        self.__broadcast_scheduler.schedule_job(BroadcastCommand.SUBSCRIBE, peer_stub.target)

    async def __subscribe_call_to_stub(self, peer_stub, peer_type):
        if self.is_support_node_function(conf.NodeFunction.Vote):
            await peer_stub.call_async(
                "Subscribe",
                loopchain_pb2.PeerRequest(
                    channel=ChannelProperty().name,
                    peer_target=ChannelProperty().peer_target, peer_type=peer_type,
                    peer_id=ChannelProperty().peer_id, group_id=ChannelProperty().group_id,
                    node_type=ChannelProperty().node_type
                ),
            )
        else:
            await self.__subscribe_call_from_citizen()

    async def __subscribe_call_from_citizen(self):
        def _handle_exception(future: asyncio.Future):
            logging.debug(f"error: {type(future.exception())}, {str(future.exception())}")
            if isinstance(future.exception(), NotImplementedError):
                asyncio.ensure_future(self.__subscribe_call_by_rest_stub(subscribe_event))

            elif isinstance(future.exception(), ConnectionError):
                logging.warning(f"Waiting for next subscribe request...")
                if self.__state_machine.state != "SubscribeNetwork":
                    self.__state_machine.subscribe_network()

        subscribe_event = asyncio.Event()
        util.logger.spam(f"try subscribe_call_by_citizen target({ChannelProperty().rest_target})")

        # try websocket connection, and handle exception in callback
        asyncio.ensure_future(self.__node_subscriber.subscribe(
            block_height=self.block_manager.get_blockchain().block_height,
            event=subscribe_event
        )).add_done_callback(_handle_exception)
        await subscribe_event.wait()

    async def __subscribe_call_by_rest_stub(self, event):
        if conf.REST_SSL_TYPE == conf.SSLAuthType.none:
            peer_target = ChannelProperty().rest_target
        else:
            peer_target = f"https://{ChannelProperty().rest_target}"

        response = None
        try:
            response = await self.__radio_station_stub.call_async(
                "Subscribe", {
                    'channel': ChannelProperty().name,
                    'peer_target': peer_target
                }
            )

        except Exception as e:
            logging.warning(f"Due to Subscription fail to RadioStation(mother peer), "
                            f"automatically retrying subscribe call")

        if response and response['response_code'] == message_code.Response.success:
            logging.debug(f"Subscription to RadioStation(mother peer) is successful.")
            event.set()
            self.start_check_last_block_rs_timer()

    def __check_last_block_to_rs(self):
        last_block = self.__radio_station_stub.call_async("GetLastBlock")
        if last_block['height'] <= self.__block_manager.get_blockchain().block_height:
            return

        # RS peer didn't announced new block
        self.stop_check_last_block_rs_timer()
        if self.__state_machine.state != "SubscribeNetwork":
            self.__state_machine.subscribe_network()

    def shutdown_peer(self, **kwargs):
        logging.debug(f"channel_service:shutdown_peer")
        StubCollection().peer_stub.sync_task().stop(message=kwargs['message'])

    def set_peer_type(self, peer_type):
        """Set peer type when peer init only

        :param peer_type:
        :return:
        """
        self.__block_manager.set_peer_type(peer_type)

    def save_peer_manager(self, peer_manager):
        """peer_list 를 leveldb 에 저장한다.

        :param peer_manager:
        """
        level_db_key_name = str.encode(conf.LEVEL_DB_KEY_FOR_PEER_LIST)

        try:
            dump = peer_manager.dump()
            level_db = self.__block_manager.get_level_db()
            level_db.Put(level_db_key_name, dump)
        except AttributeError as e:
            logging.warning("Fail Save Peer_list: " + str(e))

    async def set_peer_type_in_channel(self):
        peer_type = loopchain_pb2.PEER
        peer_leader = self.peer_manager.get_leader_peer(
            is_complain_to_rs=self.is_support_node_function(conf.NodeFunction.Vote))
        logging.debug(f"channel({ChannelProperty().name}) peer_leader: " + str(peer_leader))

        logger_preset = loggers.get_preset()
        if self.is_support_node_function(conf.NodeFunction.Vote) and ChannelProperty().peer_id == peer_leader.peer_id:
            logger_preset.is_leader = True
            logging.debug(f"Set Peer Type Leader! channel({ChannelProperty().name})")
            peer_type = loopchain_pb2.BLOCK_GENERATOR
        else:
            logger_preset.is_leader = False
        logger_preset.update_logger()

        if conf.CONSENSUS_ALGORITHM == conf.ConsensusAlgorithm.lft:
            self.consensus.leader_id = peer_leader.peer_id

        if peer_type == loopchain_pb2.BLOCK_GENERATOR:
            self.block_manager.set_peer_type(peer_type)
            self.__ready_to_height_sync(True)
        elif peer_type == loopchain_pb2.PEER:
            self.__ready_to_height_sync(False)

    def __ready_to_height_sync(self, is_leader: bool = False):
        block_chain = self.block_manager.get_blockchain()

        block_chain.init_block_chain(is_leader)
        if block_chain.block_height > -1:
            self.block_manager.rebuild_block()

    async def block_height_sync_channel(self):
        # leader 로 시작하지 않았는데 자신의 정보가 leader Peer 정보이면 block height sync 하여
        # 최종 블럭의 leader 를 찾는다.
        peer_manager = self.peer_manager
        peer_leader = peer_manager.get_leader_peer()
        self_peer_object = peer_manager.get_peer(ChannelProperty().peer_id)
        is_delay_announce_new_leader = False
        peer_old_leader = None

        if peer_leader:
            block_sync_target = peer_leader.target
            block_sync_target_stub = StubManager.get_stub_manager_to_server(
                block_sync_target,
                loopchain_pb2_grpc.PeerServiceStub,
                time_out_seconds=conf.CONNECTION_RETRY_TIMEOUT,
                ssl_auth_type=conf.GRPC_SSL_TYPE
            )
        else:
            block_sync_target = ChannelProperty().radio_station_target
            block_sync_target_stub = self.__radio_station_stub

        if block_sync_target != ChannelProperty().peer_target:
            if block_sync_target_stub is None:
                logging.warning("You maybe Older from this network... or No leader in this network!")

                is_delay_announce_new_leader = True
                peer_old_leader = peer_leader
                peer_leader = self.peer_manager.leader_complain_to_rs(
                    conf.ALL_GROUP_ID, is_announce_new_peer=False)

                if peer_leader is not None and ChannelProperty().node_type == conf.NodeType.CommunityNode:
                    block_sync_target_stub = StubManager.get_stub_manager_to_server(
                        peer_leader.target,
                        loopchain_pb2_grpc.PeerServiceStub,
                        time_out_seconds=conf.CONNECTION_RETRY_TIMEOUT,
                        ssl_auth_type=conf.GRPC_SSL_TYPE
                    )

            if self.is_support_node_function(conf.NodeFunction.Vote) and \
                    (not peer_leader or peer_leader.peer_id == ChannelProperty().peer_id):
                peer_leader = self_peer_object
                self.block_manager.set_peer_type(loopchain_pb2.BLOCK_GENERATOR)
            else:
                _, future = self.block_manager.block_height_sync(block_sync_target_stub)
                await future

                self.show_peers()

            if is_delay_announce_new_leader and ChannelProperty().node_type == conf.NodeType.CommunityNode:
                self.peer_manager.announce_new_leader(
                    peer_old_leader.peer_id,
                    peer_leader.peer_id,
                    self_peer_id=ChannelProperty().peer_id)

    def show_peers(self):
        logging.debug(f"peer_service:show_peers ({ChannelProperty().name}): ")
        for peer in self.peer_manager.get_IP_of_peers_in_group():
            logging.debug("peer_target: " + peer)

    async def reset_leader(self, new_leader_id, block_height=0):
        logging.info(f"RESET LEADER channel({ChannelProperty().name}) leader_id({new_leader_id})")
        leader_peer = self.peer_manager.get_peer(new_leader_id, None)

        if block_height > 0 and block_height != self.block_manager.get_blockchain().last_block.header.height + 1:
            util.logger.warning(f"height behind peer can not take leader role. block_height({block_height}), "
                                f"last_block.header.height("
                                f"{self.block_manager.get_blockchain().last_block.header.height})")
            return

        if leader_peer is None:
            logging.warning(f"in peer_service:reset_leader There is no peer by peer_id({new_leader_id})")
            return

        util.logger.spam(f"peer_service:reset_leader target({leader_peer.target})")

        self_peer_object = self.peer_manager.get_peer(ChannelProperty().peer_id)
        self.peer_manager.set_leader_peer(leader_peer, None)

        peer_leader = self.peer_manager.get_leader_peer()
        peer_type = loopchain_pb2.PEER

        if self_peer_object.target == peer_leader.target:
            logging.debug("Set Peer Type Leader!")
            peer_type = loopchain_pb2.BLOCK_GENERATOR
            self.state_machine.turn_to_leader()

            if conf.CONSENSUS_ALGORITHM != conf.ConsensusAlgorithm.lft:
                if conf.ENABLE_REP_RADIO_STATION:
                    self.peer_manager.announce_new_leader(
                        self.peer_manager.get_leader_peer().peer_id,
                        new_leader_id,
                        is_broadcast=True,
                        self_peer_id=ChannelProperty().peer_id
                    )
        else:
            logging.debug("Set Peer Type Peer!")
            self.state_machine.turn_to_peer()

            # 새 leader 에게 subscribe 하기
            # await self.subscribe_to_radio_station()
            await self.subscribe_to_peer(peer_leader.peer_id, loopchain_pb2.BLOCK_GENERATOR)

        self.block_manager.set_peer_type(peer_type)
        self.block_manager.epoch.set_epoch_leader(peer_leader.peer_id)

    def set_new_leader(self, new_leader_id, block_height=0):
        logging.info(f"SET NEW LEADER channel({ChannelProperty().name}) leader_id({new_leader_id})")

        # complained_leader = self.peer_manager.get_leader_peer()
        leader_peer = self.peer_manager.get_peer(new_leader_id, None)

        if block_height > 0 and block_height != self.block_manager.get_blockchain().last_block.height + 1:
            logging.warning(f"height behind peer can not take leader role.")
            return

        if leader_peer is None:
            logging.warning(f"in channel_service:set_new_leader::There is no peer by peer_id({new_leader_id})")
            return

        util.logger.spam(f"channel_service:set_new_leader::leader_target({leader_peer.target})")

        self_peer_object = self.peer_manager.get_peer(ChannelProperty().peer_id)
        self.peer_manager.set_leader_peer(leader_peer, None)

        peer_leader = self.peer_manager.get_leader_peer()

        if self_peer_object.target == peer_leader.target:
            loggers.get_preset().is_leader = True
            loggers.get_preset().update_logger()

            logging.debug("I'm Leader Peer!")
        else:
            loggers.get_preset().is_leader = False
            loggers.get_preset().update_logger()

            logging.debug("I'm general Peer!")
            # 새 leader 에게 subscribe 하기
            # await self.subscribe_to_radio_station()
            # await self.subscribe_to_peer(peer_leader.peer_id, loopchain_pb2.BLOCK_GENERATOR)

    def genesis_invoke(self, block: Block) -> ('Block', dict):
        method = "icx_sendTransaction"
        transactions = []
        for tx in block.body.transactions.values():
            tx_serializer = TransactionSerializer.new(tx.version, self.block_manager.get_blockchain().tx_versioner)
            transaction = {
                "method": method,
                "params": {
                    "txHash": tx.hash.hex()
                },
                "genesisData": tx_serializer.to_full_data(tx)
            }
            transactions.append(transaction)

        request = {
            'block': {
                'blockHeight': block.header.height,
                'blockHash': block.header.hash.hex(),
                'timestamp': block.header.timestamp
            },
            'transactions': transactions
        }
        request = convert_params(request, ParamType.invoke)
        stub = StubCollection().icon_score_stubs[ChannelProperty().name]
        response = stub.sync_task().invoke(request)
        response_to_json_query(response)

        block_builder = BlockBuilder.from_new(block, self.block_manager.get_blockchain().tx_versioner)
        block_builder.commit_state = {
            ChannelProperty().name: response['stateRootHash']
        }
        new_block = block_builder.build()
        return new_block, response["txResults"]

    def score_invoke(self, _block: Block) -> dict or None:
        method = "icx_sendTransaction"
        transactions = []
        for tx in _block.body.transactions.values():
            tx_serializer = TransactionSerializer.new(tx.version, self.block_manager.get_blockchain().tx_versioner)

            transaction = {
                "method": method,
                "params": tx_serializer.to_full_data(tx)
            }
            transactions.append(transaction)

        request = {
            'block': {
                'blockHeight': _block.header.height,
                'blockHash': _block.header.hash.hex(),
                'prevBlockHash': _block.header.prev_hash.hex() if _block.header.prev_hash else '',
                'timestamp': _block.header.timestamp
            },
            'transactions': transactions
        }
        request = convert_params(request, ParamType.invoke)
        stub = StubCollection().icon_score_stubs[ChannelProperty().name]
        response = stub.sync_task().invoke(request)
        response_to_json_query(response)

        block_builder = BlockBuilder.from_new(_block, self.__block_manager.get_blockchain().tx_versioner)
        block_builder.commit_state = {
            ChannelProperty().name: response['stateRootHash']
        }
        new_block = block_builder.build()

        return new_block, response["txResults"]

    def score_change_block_hash(self, block_height, old_block_hash, new_block_hash):
        change_hash_info = json.dumps({"block_height": block_height, "old_block_hash": old_block_hash,
                                       "new_block_hash": new_block_hash})

        stub = StubCollection().score_stubs[ChannelProperty().name]
        stub.sync_task().change_block_hash(change_hash_info)

    def score_write_precommit_state(self, block: Block):
        logging.debug(f"call score commit {ChannelProperty().name} {block.header.height} {block.header.hash.hex()}")

        request = {
            "blockHeight": block.header.height,
            "blockHash": block.header.hash.hex(),
        }
        request = convert_params(request, ParamType.write_precommit_state)

        stub = StubCollection().icon_score_stubs[ChannelProperty().name]
        stub.sync_task().write_precommit_state(request)
        return True

    def score_remove_precommit_state(self, block: Block):
        invoke_fail_info = json.dumps({"block_height": block.height, "block_hash": block.block_hash})
        stub = StubCollection().score_stubs[ChannelProperty().name]
        stub.sync_task().remove_precommit_state(invoke_fail_info)
        return True

    def get_object_has_queue_by_consensus(self):
        if conf.CONSENSUS_ALGORITHM == conf.ConsensusAlgorithm.lft:
            object_has_queue = self.__consensus
        else:
            object_has_queue = self.__block_manager

        self.start_leader_complain_timer()

        return object_has_queue

    def start_leader_complain_timer(self):
        # util.logger.debug(f"start_leader_complain_timer in channel service.")
        self.__timer_service.add_timer_convenient(timer_key=TimerService.TIMER_KEY_LEADER_COMPLAIN,
                                                  duration=conf.TIMEOUT_FOR_LEADER_COMPLAIN,
                                                  is_repeat=True, callback=self.state_machine.leader_complain)

    def stop_leader_complain_timer(self):
        # util.logger.debug(f"stop_leader_complain_timer in channel service.")
        self.__timer_service.stop_timer(TimerService.TIMER_KEY_LEADER_COMPLAIN)

    def start_subscribe_timer(self):
        self.__timer_service.add_timer_convenient(timer_key=TimerService.TIMER_KEY_SUBSCRIBE,
                                                  duration=conf.SUBSCRIBE_RETRY_TIMER,
                                                  is_repeat=True, callback=self.subscribe_network)

    def stop_subscribe_timer(self):
        self.__timer_service.stop_timer(TimerService.TIMER_KEY_SUBSCRIBE)

    def start_check_last_block_rs_timer(self):
        self.__timer_service.add_timer_convenient(
            timer_key=TimerService.TIMER_KEY_GET_LAST_BLOCK_KEEP_CITIZEN_SUBSCRIPTION,
            duration=conf.GET_LAST_BLOCK_TIMER, is_repeat=True, callback=self.__check_last_block_to_rs)

    def stop_check_last_block_rs_timer(self):
        self.__timer_service.stop_timer(TimerService.TIMER_KEY_GET_LAST_BLOCK_KEEP_CITIZEN_SUBSCRIPTION)

    def start_shutdown_timer(self):
        error = f"Shutdown by Subscribe retry timeout({conf.SHUTDOWN_TIMER} sec)"
        self.__timer_service.add_timer_convenient(timer_key=TimerService.TIMER_KEY_SHUTDOWN_WHEN_FAIL_SUBSCRIBE,
                                                  duration=conf.SHUTDOWN_TIMER, callback=self.shutdown_peer,
                                                  callback_kwargs={"message": error})

    def stop_shutdown_timer(self):
        self.__timer_service.stop_timer(TimerService.TIMER_KEY_SHUTDOWN_WHEN_FAIL_SUBSCRIBE)
