import argparse
import asyncio
import bitmex
import configparser
import iso8601
import json
import logging
import sys
import time, threading
import traceback2
import websockets
import signal

from datetime import datetime
from datetime import timezone as timezone
from MySqlDataStore import get_mysql_connection

# Deal with command-line options.
args_parser = argparse.ArgumentParser()
args_parser.add_argument('--instance', type=int, required=True, choices=range(0,5), help='non-negative integer that specifies production instance of trading bot')
args = args_parser.parse_args()

# Read configuration.
config = configparser.ConfigParser()
config.read(f'config/bitmex_bot_{args.instance}.ini')
defaults = config['DEFAULT']

# Logging.
logger = logging.getLogger("bitmex-trade-buckets-logger")
logger.setLevel(logging.INFO)

# Add file and console handlers.
logger.addHandler(logging.FileHandler(defaults.get('log.buckets.outfile')))
logger.addHandler(logging.StreamHandler())

# Open client-access to bitmex API.
bitmex_client = bitmex.bitmex(
	test=defaults.getboolean('bitmex.tradebuckets.api.test'),
	api_key=defaults.get('bitmex.tradebuckets.api.key'),
	api_secret=defaults.get('bitmex.tradebuckets.api.secret')
)

# Get connection to MySQL.
logger.info('Opening connection with MySQL')
try:
	connection = get_mysql_connection(defaults)
except Exception:
	logger.error(traceback2.format_exc())
	sys.exit(1)

def insert_trade_bucket(trade_bucket):
	timestamp = trade_bucket['timestamp']
	symbol = trade_bucket['symbol']
	open_px = trade_bucket['open'] if 'open' in trade_bucket else None
	high_px = trade_bucket['high'] if 'high' in trade_bucket else None
	low_px = trade_bucket['low'] if 'low' in trade_bucket else None
	close_px = trade_bucket['close'] if 'close' in trade_bucket else None
	trades = trade_bucket['trades'] if 'trades' in trade_bucket else None
	volume = trade_bucket['volume'] if 'volume' in trade_bucket else None
	vwap = trade_bucket['vwap'] if 'vwap' in trade_bucket else None
	lastSize = trade_bucket['lastSize'] if 'lastSize' in trade_bucket else None
	turnover = trade_bucket['turnover'] if 'turnover' in trade_bucket else None
	homeNotional = trade_bucket['homeNotional'] if 'homeNotional' in trade_bucket else None
	foreignNotional = trade_bucket['foreignNotional'] if 'foreignNotional' in trade_bucket else None

	timestamp_ms = timestamp.timestamp()
	try:
		with connection.cursor() as cursor:
			sql = "INSERT INTO `tradeBin1m` (`timestamp_dt`, `symbol`, `open_px`, `high_px`, `low_px`, `close_px`, `trades`, `volume`, `vwap`, `last_size`, `turnover`, `home_notional`, `foreign_notional`) VALUES (FROM_UNIXTIME(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
			logger.info(f'timestamp_dt:{timestamp} symbol:{symbol} open_px:{open_px} high_px:{high_px} low_px:{low_px} close_px:{close_px} trades:{trades} volume:{volume} vwap:{vwap} lastSize:{lastSize} turnover:{turnover} homeNotional:{homeNotional} foreignNotional:{foreignNotional}')
			cursor.execute(sql, (timestamp_ms, symbol, open_px, high_px, low_px, close_px, trades, volume, vwap, lastSize, turnover, homeNotional, foreignNotional))
			connection.commit()
	except Exception:
		logger.error(traceback2.format_exc())

def get_max_timestamp_of_trade_buckets():
	logger.info('Reading max timestamp of existing trade buckets')
	max_timestamp_dt = None
	try:
		with connection.cursor() as cursor:
			sql = "SELECT MAX(timestamp_dt) AS max_timestamp_dt FROM tradeBin1m"
			cursor.execute(sql)
			result = cursor.fetchone()
			max_timestamp_dt = None if not result else result['max_timestamp_dt']
	except Exception:
		logger.error(traceback2.format_exc())
	return max_timestamp_dt.timestamp() if max_timestamp_dt else None

# By default, fill gaps through last 21 days in our data store for trade-buckets.
# XXX If we have been offline for > 21 days, then horizon is extended further out
# to begin right after the last historical trade bucket. (Maybe we don't need to do this.)
def fill_gaps(horizon=21*24*60*60):
	now = datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()
	logger.info(f'Fill gaps in trade buckets through {now}')

	max_timestamp_dt = get_max_timestamp_of_trade_buckets()
	if max_timestamp_dt == None:
		logger.info('Could not find max(timestamp_dt) for trade buckets. Default to now.')
		max_timestamp_dt = now

	twenty_one_days_ago = now - horizon
	start_time = min(twenty_one_days_ago, max_timestamp_dt) + 60

	while start_time <= now:
		# start_time_str should have format "2020-01-02 18:03", understood to be in UTC timezone.
		start_time_dt = datetime.fromtimestamp(start_time, tz=timezone.utc)
		start_time_str = start_time_dt.strftime("%Y-%m-%d %H:%M")

		# Query 2 hours of trades at a time.  That's a 120 samples per API query (hence, count=120).
		try:
			trades = bitmex_client.Trade.Trade_getBucketed(symbol="XBTUSD", binSize="1m", partial=False, reverse=False, count=120, filter=json.dumps({"startTime": start_time_str})).result()
			logger.info(f'start_time_str:{start_time_str} ntrades:{len(trades[0])}')
		except Exception:
			logger.error(traceback2.format_exc())
			trades = None

		# Archive historical trades, if any. Advance the clock 2 hours, but only if the last API request succeeded.
		if trades:
			for k in range(0, len(trades[0])): insert_trade_bucket(trades[0][k])
			start_time = start_time + 2*60*60

		# Sleep to avoid API rate limit violations.
		time.sleep(2)
		# Update NOW in order to capture any trade buckets that may have completed since we started filling gaps.
		now = datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()

async def capture_data():
	# Always first fill any gaps since our last query.
	fill_gaps()

	rcvd_tradeBin1m_partial = False
	when_outage_occurred = None

	# Always scrape trade-buckets from production API.
	uri = "wss://www.bitmex.com/realtime?subscribe=tradeBin1m:XBTUSD"
	async with websockets.connect(uri) as websocket:
		while True:
			# Reconnect if not open.
			if not websocket.open:
				websocket = await websockets.connect(uri)

			if websocket.open: when_outage_occurred = None
			try:
				data = await websocket.recv()
			except Exception:
				logger.error(traceback2.format_exc())
				if not when_outage_occurred:
					when_outage_occurred = datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()
				# Cushion gap to fill by 5 minutes before occurrence of outage.
				now = datetime.utcnow().replace(tzinfo=timezone.utc).timestamp()
				horizon = now - when_outage_occurred + 5*60
				fill_gaps(horizon)
				continue

			# Grok JSON.
			data = json.loads(data)

			# Skip over "info" and "success" acknowledgments received after subscription.
			if 'table' not in data or 'action' not in data:
				continue

			if data['table'] == 'tradeBin1m' and data['action'] == 'partial':
				rcvd_tradeBin1m_partial = True
				continue

			# Subscription specs say that we should skip over any messages before both partials for trade/quote are received.
			if not rcvd_tradeBin1m_partial:
				continue

			# Sanity-check presence of "data" attribute, which houses the trade/quote payload.
			if 'data' not in data or len(data['data']) <= 0:
				continue

			# There should only be a single trade-bucket per minute.
			last_insert = data['data'][-1]

			# (timestamp, symbol) is our primary key for trade buckets, so skip any records in which those are missing.
			if 'timestamp' not in last_insert \
				or 'symbol' not in last_insert or last_insert['symbol'] != 'XBTUSD':
				continue

			last_insert['timestamp'] = iso8601.parse_date(last_insert['timestamp'])
			insert_trade_bucket(last_insert)

async def gracefully_finish(loop):
    # Gracefully shut down database connection on interrupt.
    if connection != None:
        connection.close()

    # Gracefully terminate ongoing tasks.
    tasks = [task for task in asyncio.Task.all_tasks()]
    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.info("Task cancelled: %s" % task)   
    loop.stop()

event_loop = asyncio.get_event_loop()
try:
    event_loop.run_until_complete(capture_data())
except Exception:
    logger.error(traceback2.format_exc())
finally:
    event_loop.run_until_complete(gracefully_finish(event_loop))
    event_loop.close()
