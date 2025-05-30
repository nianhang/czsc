# -*- coding: utf-8 -*-
"""
author: zengbin93
email: zeng_bin8888@163.com
create_dt: 2023/9/24 15:19
describe: 策略持仓权重管理
"""
import os
import time
import json
import redis
import threading
import pandas as pd
from loguru import logger
from datetime import datetime


class RedisWeightsClient:
    """策略持仓权重收发客户端"""

    version = "V240303"

    def __init__(self, strategy_name, redis_url=None, connection_pool=None, send_heartbeat=True, **kwargs):
        """
        :param strategy_name: str, 策略名
        :param redis_url: str, redis连接字符串, 默认为None, 即从环境变量 RWC_REDIS_URL 中读取

            For example::

                redis://[[username]:[password]]@localhost:6379/0
                rediss://[[username]:[password]]@localhost:6379/0
                unix://[username@]/path/to/socket.sock?db=0[&password=password]

            Three URL schemes are supported:

            - `redis://` creates a TCP socket connection. See more at:
            <https://www.iana.org/assignments/uri-schemes/prov/redis>
            - `rediss://` creates a SSL wrapped TCP socket connection. See more at:
            <https://www.iana.org/assignments/uri-schemes/prov/rediss>
            - ``unix://``: creates a Unix Domain Socket connection.

        :param connection_pool: redis.BlockingConnectionPool, redis连接池，默认为None

            如果传入了 redis_url，则会自动创建一个连接池，否则需要传入一个连接池；如果传入了连接池，则会忽略 redis_url。

        :param send_heartbeat: boolean, 是否发送心跳，默认为True

            如果为True，会在后台启动一个线程，每15秒向redis发送一次心跳，用于检测策略是否存活。
            推荐在写入数据时设置为True，读取数据时设置为False，避免无用的心跳。

        :param kwargs: dict, 其他参数

            - key_prefix: str, redis中key的前缀，默认为 Weights
            - heartbeat_prefix: str, 心跳key的前缀，默认为 heartbeat
        """
        self.strategy_name = strategy_name
        self.key_prefix = kwargs.get("key_prefix", "Weights")

        if connection_pool:
            thread_safe_pool = connection_pool
            self.redis_url = connection_pool.connection_kwargs.get("url")
            logger.info(f"{strategy_name} {self.key_prefix}: 使用传入的 redis 连接池")

        else:
            self.redis_url = redis_url if redis_url else os.getenv("RWC_REDIS_URL")
            thread_safe_pool = redis.BlockingConnectionPool.from_url(self.redis_url, decode_responses=True)
            logger.info(f"{strategy_name} {self.key_prefix}: 使用环境变量 RWC_REDIS_URL 创建 redis 连接池")

        assert isinstance(thread_safe_pool, redis.BlockingConnectionPool), "redis连接池创建失败"

        self.r = redis.Redis(connection_pool=thread_safe_pool)
        self.lua_publish = RedisWeightsClient.register_lua_publish(self.r)
        self.heartbeat_prefix = kwargs.get("heartbeat_prefix", "heartbeat")

        if send_heartbeat:
            self.heartbeat_client = redis.Redis(connection_pool=thread_safe_pool)
            self.heartbeat_thread = threading.Thread(target=self.__heartbeat, daemon=True)
            self.heartbeat_thread.start()

    def set_metadata(self, base_freq, description, author, outsample_sdt, **kwargs):
        """设置策略元数据"""
        key = f"{self.key_prefix}:META:{self.strategy_name}"
        overwrite = kwargs.pop("overwrite", False)
        if self.r.exists(key):
            if not overwrite:
                logger.warning(f"已存在 {self.strategy_name} 的元数据，如需覆盖请设置 overwrite=True")
                return
            else:
                self.r.delete(key)
                logger.warning(f"删除 {self.strategy_name} 的元数据，重新写入")

        outsample_sdt = pd.to_datetime(outsample_sdt).strftime("%Y%m%d")
        meta = {
            "name": self.strategy_name,
            "base_freq": base_freq,
            "key_prefix": self.key_prefix,
            "description": description,
            "author": author,
            "outsample_sdt": outsample_sdt,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "kwargs": json.dumps(kwargs),
        }
        self.r.hset(key, mapping=meta)
        self.r.sadd(f"{self.key_prefix}:StrategyNames", self.strategy_name)

    def update_last(self, **kwargs):
        """设置策略最近一次更新时间，以及更新参数【可选】"""
        key = f"{self.key_prefix}:LAST:{self.strategy_name}"
        last = {
            "name": self.strategy_name,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "kwargs": json.dumps(kwargs),
        }
        self.r.hset(key, mapping=last)

        logger.info(f"更新 {key} 的 last 时间")

    @property
    def metadata(self):
        """获取策略元数据"""
        key = f"{self.key_prefix}:META:{self.strategy_name}"
        return self.r.hgetall(key)

    @property
    def heartbeat_time(self):
        """获取策略的最近一次心跳时间"""
        key = f"{self.key_prefix}:{self.heartbeat_prefix}:{self.strategy_name}"
        return pd.to_datetime(self.r.get(key))

    def get_last_times(self, symbols=None):
        """获取所有品种上策略最近一次发布信号的时间

        :param symbols: list, 品种列表, 默认为None, 即获取所有品种
        :return: dict, {symbol: datetime}，如{'SFIF9001': datetime(2021, 9, 24, 15, 19, 0)}
        """
        if isinstance(symbols, str):
            row = self.r.hgetall(f"{self.key_prefix}:{self.strategy_name}:{symbols}:LAST")
            return pd.to_datetime(row["dt"]) if row else None  # type: ignore

        symbols = symbols if symbols else self.get_symbols()
        with self.r.pipeline() as pipe:
            for symbol in symbols:
                pipe.hgetall(f"{self.key_prefix}:{self.strategy_name}:{symbol}:LAST")
            rows = pipe.execute()
        return {x["symbol"]: pd.to_datetime(x["dt"]) for x in rows}

    def publish(self, symbol, dt, weight, price=0, ref=None, overwrite=False):
        """发布单个策略持仓权重

        :param symbol: str, eg; SFIF9001
        :param dt: py_datetime or pandas Timestamp
        :param weight: float, 信号值
        :param price: float, 产生信号时的价格
        :param ref: dict, 自定义数据
        :param overwrite: boolean, 是否覆盖已有记录
        :return: 成功发布信号的条数
        """
        if not isinstance(dt, datetime):
            dt = pd.to_datetime(dt)

        if not overwrite:
            last_dt = self.get_last_times(symbol)
            if last_dt is not None and dt <= last_dt:  # type: ignore
                logger.warning(f"不允许重复写入，已过滤 {symbol} {dt} 的重复信号")
                return 0

        udt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        key = f'{self.key_prefix}:{self.strategy_name}:{symbol}:{dt.strftime("%Y%m%d%H%M%S")}'
        ref = ref if ref else "{}"
        ref_str = json.dumps(ref) if isinstance(ref, dict) else ref
        return self.lua_publish(keys=[key], args=[1 if overwrite else 0, udt, weight, price, ref_str])

    def publish_dataframe(self, df, overwrite=False, batch_size=10000):
        """批量发布多个策略信号

        :param df: pandas.DataFrame, 必需包含['symbol', 'dt', 'weight']列,
                可选['price', 'ref']列, 如没有price则写0, dtype同publish方法
        :param overwrite: boolean, 是否覆盖已有记录
        :param batch_size: int, 每次发布的最大数量
        :return: 成功发布信号的条数
        """
        df = df.copy()
        df["dt"] = pd.to_datetime(df["dt"])
        logger.info(f"输入数据中有 {len(df)} 条权重信号")

        # 去除单个品种下相邻时间权重相同的数据
        _res = []
        for _, dfg in df.groupby("symbol"):
            dfg = dfg.sort_values("dt", ascending=True).reset_index(drop=True)
            dfg = dfg[dfg["weight"].diff().fillna(1) != 0].copy()
            _res.append(dfg)
        df = pd.concat(_res, ignore_index=True)
        df = df.sort_values(["dt"]).reset_index(drop=True)
        logger.info(f"去除单个品种下相邻时间权重相同的数据后，剩余 {len(df)} 条权重信号")

        if "price" not in df.columns:
            df["price"] = 0
        if "ref" not in df.columns:
            df["ref"] = "{}"

        if not overwrite:
            raw_count = len(df)
            _time = self.get_last_times()
            _data = []
            for symbol, dfg in df.groupby("symbol"):
                last_dt = _time.get(symbol)
                if last_dt is not None:
                    dfg = dfg[dfg["dt"] > last_dt]
                _data.append(dfg)
            df = pd.concat(_data, ignore_index=True)
            logger.info(f"不允许重复写入，已过滤 {raw_count - len(df)} 条重复信号")

        keys, args = [], []
        for row in df[["symbol", "dt", "weight", "price", "ref"]].to_numpy():
            key = f'{self.key_prefix}:{self.strategy_name}:{row[0]}:{row[1].strftime("%Y%m%d%H%M%S")}'
            keys.append(key)

            args.append(row[2])
            args.append(row[3])
            ref = row[4]
            args.append(json.dumps(ref) if isinstance(ref, dict) else ref)

        udt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        overwrite = 1 if overwrite else 0

        pub_cnt = 0
        len_keys = len(keys)
        for i in range(0, len_keys, batch_size):
            if i + batch_size < len_keys:
                tmp_keys = keys[i : i + batch_size]
                tmp_args = [overwrite, udt] + args[3 * i : 3 * (i + batch_size)]
            else:
                tmp_keys = keys[i:len_keys]
                tmp_args = [overwrite, udt] + args[3 * i : 3 * len_keys]
            logger.info(f"索引 {i}，即将发布 {len(tmp_keys)} 条权重信号")
            pub_cnt += self.lua_publish(keys=tmp_keys, args=tmp_args)
            logger.info(f"已完成 {pub_cnt} 次发布")

        self.update_last()
        return pub_cnt

    def __heartbeat(self):
        while True:
            key = f"{self.key_prefix}:{self.heartbeat_prefix}:{self.strategy_name}"
            try:
                self.heartbeat_client.set(key, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            except Exception as e:
                logger.error(f"心跳发送失败：{e}")
            time.sleep(15)

    def get_keys(self, pattern) -> list:
        """获取 redis 中指定 pattern 的 keys"""
        k = self.r.keys(pattern)
        return k if k else []  # type: ignore

    def clear_all(self, with_human=True):
        """删除该策略所有记录"""
        keys = self.get_keys(f"{self.key_prefix}:{self.strategy_name}*")
        keys.append(f"{self.key_prefix}:META:{self.strategy_name}")
        keys.append(f"{self.key_prefix}:LAST:{self.strategy_name}")
        keys.append(f"{self.key_prefix}:{self.heartbeat_prefix}:{self.strategy_name}")

        if len(keys) == 0:
            logger.warning(f"{self.strategy_name} 没有记录")
            return

        if with_human:
            human = input(f"{self.strategy_name} 即将删除 {len(keys)} 条记录，是否确认？(y/n):")
            if human.lower() != "y":
                logger.warning(f"{self.strategy_name} 删除操作已取消")
                return

        self.r.delete(*keys)  # type: ignore
        cong = self.r.srem(f"{self.key_prefix}:StrategyNames", self.strategy_name)
        logger.info(f"{self.strategy_name} 删除了 {len(keys)} 条记录, {cong} 条策略名")

    @staticmethod
    def register_lua_publish(client):
        lua_body = """
local overwrite = ARGV[1]
local update_time = ARGV[2]
local cnt = 0
local ret
for i = 1, #KEYS do
    local key = KEYS[i]
    local sig = ARGV[3 + 3 * (i - 1)]
    local price = ARGV[4 + 3 * (i - 1)]
    local ref_str = ARGV[5 + 3 * (i - 1)]
    local split_str = {}
    key:gsub('[^:]+', function(s) table.insert(split_str, s) end)
    local model_key = split_str[1] .. ':' .. split_str[2] .. ':' .. split_str[3]

    local if_pass = true
    if overwrite ~= '0' then
        if_pass = false
    else
        local pos = redis.call('HGET', model_key .. ':LAST', 'weight')
        if not pos or math.abs(tonumber(sig) - tonumber(pos)) > 0.00001 then
            if_pass = false
        end
    end

    if not if_pass then
        local strategy_name, symbol, action_time = split_str[2], split_str[3], split_str[4]
        local at_str = string.sub(action_time, 1, 4) .. '-' .. string.sub(action_time, 5, 6) .. '-' ..
            string.sub(action_time, 7, 8) .. ' ' .. string.sub(action_time, 9, 10) .. ':' ..
            string.sub(action_time, 11, 12) .. ':' .. string.sub(action_time, 13, 14)
        redis.call('ZADD', model_key, tonumber(action_time), key)
        local ret1 = redis.call('HMSET', key, 'symbol', symbol, 'weight', sig, 'dt', at_str, 'update_time', update_time, 'price', price, 'ref', ref_str)
        local ret2 = redis.call('HMSET', key:gsub(action_time, 'LAST'), 'symbol', symbol, 'weight', sig, 'dt', at_str, 'update_time', update_time, 'price', price, 'ref', ref_str)
        if ret1.ok and ret2.ok then
            cnt = cnt + 1
            local pubKey = 'PUBSUB:' .. split_str[1] .. ':' .. strategy_name .. ':' .. symbol
            redis.call('PUBLISH', pubKey, key .. ':' .. sig .. ':' .. price .. ':' .. ref_str)
        end
    end
end
return cnt
"""
        return client.register_script(lua_body)

    def get_symbols(self):
        """获取策略交易的品种列表"""
        keys = self.get_keys(f"{self.key_prefix}:{self.strategy_name}:*:LAST")
        symbols = {x.split(":")[2] for x in keys}  # type: ignore
        return list(symbols)

    def get_last_weights(self, symbols=None, ignore_zero=True, lua=True):
        """获取最近的持仓权重

        :param symbols: list, 品种列表
        :param ignore_zero: boolean, 是否忽略权重为0的品种
        :param lua: boolean, 是否使用 lua 脚本获取，默认为True
            如果要全量获取，推荐使用 lua 脚本，速度更快；如果要获取指定 symbols，不推荐使用 lua 脚本。
        :return: pd.DataFrame
        """
        if lua:
            lua_script = """
            local keys = redis.call('KEYS', ARGV[1])
            local results = {}
            for i=1, #keys do
                results[i] = redis.call('HGETALL', keys[i])
            end
            return results
            """
            key_pattern = self.key_prefix + ":" + self.strategy_name + ":*:LAST"
            results = self.r.eval(lua_script, 0, key_pattern)
            rows = [dict(zip(r[::2], r[1::2])) for r in results]  # type: ignore
            if symbols:
                rows = [r for r in rows if r["symbol"] in symbols]

        else:
            symbols = symbols if symbols else self.get_symbols()
            with self.r.pipeline() as pipe:
                for symbol in symbols:
                    pipe.hgetall(f"{self.key_prefix}:{self.strategy_name}:{symbol}:LAST")
                rows = pipe.execute()

        dfw = pd.DataFrame(rows)
        dfw["weight"] = dfw["weight"].astype(float)
        dfw["dt"] = pd.to_datetime(dfw["dt"])
        if ignore_zero:
            dfw = dfw[dfw["weight"] != 0].copy().reset_index(drop=True)
        dfw = dfw.sort_values(["dt", "symbol"]).reset_index(drop=True)
        return dfw

    def get_hist_weights(self, symbol, sdt, edt) -> pd.DataFrame:
        """获取单个品种的持仓权重历史数据

        :param symbol: str, 品种代码
        :param sdt: str, 开始时间, eg: 20210924 10:19:00
        :param edt: str, 结束时间, eg: 20220924 10:19:00
        :return: pd.DataFrame
        """
        start_score = pd.to_datetime(sdt).strftime("%Y%m%d%H%M%S")
        end_score = pd.to_datetime(edt).strftime("%Y%m%d%H%M%S")
        model_key = f"{self.key_prefix}:{self.strategy_name}:{symbol}"
        key_list = self.r.zrangebyscore(model_key, start_score, end_score)

        if len(key_list) == 0:
            logger.warning(f"no history weights: {symbol} - {sdt} - {edt}")
            return pd.DataFrame()

        with self.r.pipeline() as pipe:
            for key in key_list:
                pipe.hmget(key, "weight", "price", "ref")
            rows = pipe.execute()

        weights = []
        for i in range(len(key_list)):
            dt = pd.to_datetime(key_list[i].split(":")[-1])
            weight, price, ref = rows[i]
            weight = weight if weight is None else float(weight)
            price = price if price is None else float(price)
            try:
                ref = json.loads(ref)
            except Exception as e:
                ref = ref
            weights.append((self.strategy_name, symbol, dt, weight, price, ref))

        dfw = pd.DataFrame(weights, columns=["strategy_name", "symbol", "dt", "weight", "price", "ref"])
        dfw = dfw.sort_values("dt").reset_index(drop=True)
        return dfw

    def get_all_weights(self, sdt=None, edt=None, **kwargs) -> pd.DataFrame:
        """获取所有权重数据

        :param sdt: str, 开始时间, eg: 20210924 10:19:00
        :param edt: str, 结束时间, eg: 20220924 10:19:00
        :return: pd.DataFrame
        """
        lua_script = """
        local keys = redis.call('KEYS', ARGV[1])
        local results = {}
        for i=1, #keys do
            local last_part = keys[i]:match('([^:]+)$')
            if #last_part == 14 and tonumber(last_part) ~= nil then
                results[#results + 1] = redis.call('HGETALL', keys[i])
            end
        end
        return results
        """
        key_pattern = self.key_prefix + ":" + self.strategy_name + ":*:*"
        results = self.r.eval(lua_script, 0, key_pattern)
        results = [dict(zip(r[::2], r[1::2])) for r in results]  # type: ignore

        df = pd.DataFrame(results)
        df["dt"] = pd.to_datetime(df["dt"])
        df["weight"] = df["weight"].astype(float)
        df = df.sort_values(["dt", "symbol"]).reset_index(drop=True)
        # df 中的columns：['symbol', 'weight', 'dt', 'update_time', 'price', 'ref']

        df1 = pd.pivot_table(df, index="dt", columns="symbol", values="weight").sort_index().ffill().fillna(0)
        df1 = pd.melt(df1.reset_index(), id_vars="dt", value_vars=df1.columns, value_name="weight")  # type: ignore

        # 加上 df 中的 update_time 信息
        df1 = df1.merge(df[["dt", "symbol", "update_time"]], on=["dt", "symbol"], how="left")
        df1 = df1.sort_values(["symbol", "dt"]).reset_index(drop=True)
        for _, dfg in df1.groupby("symbol"):
            df1.loc[dfg.index, "update_time"] = dfg["update_time"].ffill().bfill()

        if sdt:
            df1 = df1[df1["dt"] >= pd.to_datetime(sdt)].reset_index(drop=True)
        if edt:
            df1 = df1[df1["dt"] <= pd.to_datetime(edt)].reset_index(drop=True)
        df1 = df1.sort_values(["dt", "symbol"]).reset_index(drop=True)
        return df1


def clear_strategy(strategy_name, redis_url=None, connection_pool=None, key_prefix="Weights", **kwargs):
    """删除策略所有记录

    :param strategy_name: str, 策略名
    :param redis_url: str, redis连接字符串, 默认为None, 即从环境变量 RWC_REDIS_URL 中读取
    :param connection_pool: redis.ConnectionPool, redis连接池
    :param key_prefix: str, redis中key的前缀，默认为 Weights
    :param kwargs: dict, 其他参数
    """
    with_human = kwargs.pop("with_human", True)
    rwc = RedisWeightsClient(
        strategy_name,
        redis_url=redis_url,
        connection_pool=connection_pool,
        key_prefix=key_prefix,
        send_heartbeat=False,
        **kwargs,
    )
    rwc.clear_all(with_human)


def get_strategy_weights(strategy_name, redis_url=None, connection_pool=None, key_prefix="Weights", **kwargs):
    """获取策略的持仓权重

    :param strategy_name: str, 策略名
    :param redis_url: str, redis连接字符串, 默认为None, 即从环境变量 RWC_REDIS_URL 中读取
    :param connection_pool: redis.ConnectionPool, redis连接池
    :param key_prefix: str, redis中key的前缀，默认为 Weights
    :param kwargs: dict, 其他参数

        - symbols : list, 品种列表, 默认为None, 即获取所有品种的权重
        - sdt : str, 开始时间, eg: 20210924 10:19:00
        - edt : str, 结束时间, eg: 20220924 10:19:00
        - only_last : boolean, 是否只保留每个品种最近一次权重，默认为False

    :return: pd.DataFrame
    """
    kwargs.pop("send_heartbeat", None)
    rwc = RedisWeightsClient(
        strategy_name,
        redis_url=redis_url,
        connection_pool=connection_pool,
        key_prefix=key_prefix,
        send_heartbeat=False,
        **kwargs,
    )
    sdt = kwargs.get("sdt")
    edt = kwargs.get("edt")
    symbols = kwargs.get("symbols")
    only_last = kwargs.get("only_last", False)

    if only_last:
        # 保留每个品种最近一次权重
        df = rwc.get_last_weights(ignore_zero=False)
        return df

    df = rwc.get_all_weights(sdt=sdt, edt=edt)
    if symbols:
        # 保留指定品种的权重
        not_in = [x for x in symbols if x not in df["symbol"].unique()]
        if not_in:
            logger.warning(f"{strategy_name} 中没有 {not_in} 的权重记录")

        df = df[df["symbol"].isin(symbols)].reset_index(drop=True)

    return df


def get_strategy_mates(redis_url=None, connection_pool=None, key_pattern="Weights:META:*", **kwargs):
    """获取Redis中的策略元数据

    :param redis_url: str, redis连接字符串, 默认为None, 即从环境变量 RWC_REDIS_URL 中读取
    :param connection_pool: redis.ConnectionPool, redis连接池
    :param key_pattern: str, redis中key的pattern，默认为 Weights:META:*
    :param kwargs: dict, 其他参数
    :return: pd.DataFrame
    """
    heartbeat_prefix = kwargs.get("heartbeat_prefix", "heartbeat")

    if connection_pool:
        r = redis.Redis(connection_pool=connection_pool)
    else:
        redis_url = redis_url if redis_url else os.getenv("RWC_REDIS_URL")
        r = redis.Redis.from_url(redis_url, decode_responses=True)

    rows = []
    for key in r.keys(key_pattern):  # type: ignore
        meta = r.hgetall(key)
        if not meta:
            logger.warning(f"{key} 没有策略元数据")
            continue

        meta["heartbeat_time"] = r.get(f"{meta['key_prefix']}:{heartbeat_prefix}:{meta['name']}")  # type: ignore
        rows.append(meta)

    if len(rows) == 0:
        logger.warning(f"{key_pattern} 下没有策略元数据")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["update_time"] = pd.to_datetime(df["update_time"])
    df["heartbeat_time"] = pd.to_datetime(df["heartbeat_time"])
    df = df.sort_values("name").reset_index(drop=True)

    r.close()
    return df


def get_heartbeat_time(strategy_name=None, redis_url=None, connection_pool=None, key_prefix="Weights", **kwargs):
    """获取策略的最近一次心跳时间

    :param strategy_name: str, 策略名，默认为None, 即获取所有策略的心跳时间
    :param redis_url: str, redis连接字符串, 默认为None, 即从环境变量 RWC_REDIS_URL 中读取
    :param connection_pool: redis.ConnectionPool, redis连接池
    :param key_prefix: str, redis中key的前缀，默认为 Weights
    :param kwargs: dict, 其他参数

            - heartbeat_prefix: str, 心跳key的前缀，默认为 heartbeat

    :return: str, 最近一次心跳时间
    """
    if connection_pool:
        r = redis.Redis(connection_pool=connection_pool)
    else:
        redis_url = redis_url if redis_url else os.getenv("RWC_REDIS_URL")
        r = redis.Redis.from_url(redis_url, decode_responses=True)

    if not strategy_name:
        dfm = get_strategy_mates(
            redis_url=redis_url, connection_pool=connection_pool, key_pattern=f"{key_prefix}:META:*"
        )
        if len(dfm) == 0:
            logger.warning(f"{key_prefix} 下没有策略元数据")
            return None
        strategy_names = dfm["name"].unique().tolist()
    else:
        strategy_names = [strategy_name]

    heartbeat_prefix = kwargs.get("heartbeat_prefix", "heartbeat")
    res = {}
    for sn in strategy_names:
        hdt = r.get(f"{key_prefix}:{heartbeat_prefix}:{sn}")
        if hdt:
            res[sn] = pd.to_datetime(hdt)
        else:
            res[sn] = None
            logger.warning(f"{sn} 没有心跳时间")
    r.close()
    return res


def get_strategy_names(redis_url=None, connection_pool=None, key_prefix="Weights"):
    """获取所有策略名的列表.

    :param redis_url: str, redis连接字符串, 默认为None, 即从环境变量 RWC_REDIS_URL 中读取
    :param connection_pool: redis.ConnectionPool, redis连接池
    :param key_prefix: str, redis中key的前缀，默认为 Weights
    :return: list, 所有策略名
    """

    if connection_pool:
        r = redis.Redis(connection_pool=connection_pool)
    else:
        redis_url = redis_url if redis_url else os.getenv("RWC_REDIS_URL")
        r = redis.Redis.from_url(redis_url, decode_responses=True)

    rs = r.smembers(f"{key_prefix}:StrategyNames")
    return list(rs)


def get_strategy_latest(redis_url=None, connection_pool=None, key_prefix="Weights"):
    """获取所有策略的最新持仓

    :param redis_url: str, redis连接字符串, 默认为None, 即从环境变量 RWC_REDIS_URL 中读取
    :param connection_pool: redis.ConnectionPool, redis连接池
    :param key_prefix: str, redis中key的前缀，默认为 Weights
    :return: pd.DataFrame, 最新持仓数据
    """
    if connection_pool:
        r = redis.Redis(connection_pool=connection_pool)
    else:
        redis_url = redis_url if redis_url else os.getenv("RWC_REDIS_URL")
        r = redis.Redis.from_url(redis_url, decode_responses=True)

    keys = r.keys(f"{key_prefix}:*:LAST")
    pipeline = r.pipeline()
    for key in keys:
        pipeline.hgetall(key)
    fetched_data = pipeline.execute()

    results = []
    for key, data in zip(keys, fetched_data):
        strategy = key.split(":")[1]
        data["strategy"] = strategy
        results.append(data)

    df = pd.DataFrame(results)
    df["weight"] = df["weight"].astype(float)
    df = df[["dt", "strategy", "symbol", "weight", "update_time"]]
    return df
