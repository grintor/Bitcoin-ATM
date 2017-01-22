import picamera
import multiprocessing
import io
import zbar
from PIL import Image
from threading import Timer
from hashlib import sha256
from pycoin.key import Key
from pycoin.encoding import is_valid_wif, is_valid_bitcoin_address
from pycoin.services import spendables_for_address
from pycoin.tx.tx_utils import create_signed_tx, create_tx
from pycoin.services.blockchain_info import BlockchainInfoProvider
from pycoin.convention import tx_fee

import urllib
import json
import statistics
import serial
import array
import time
import math
import os
import sys
import shelve
from collections import defaultdict

tx_fee.TX_FEE_PER_THOUSAND_BYTES = 0
to_cam = multiprocessing.JoinableQueue(1)
from_cam = multiprocessing.JoinableQueue(1)


os.environ["PYCOIN_BTC_PROVIDERS"] = "blockchain.info blockr.io blockexplorer.com"

ourPrivateKey = "KzFEXXc1F3U1234567896cyHvnLMieFRT2oWMnW6tqx6G2"
cashAcceptorOn = False # global variable to tell acceptCash() if it should keep looping or not

txdb = shelve.open("txdb.shelve", writeback=True)
if 'receiving' not in txdb:
	print "the txdb doesn't exist. building it now"
	txdb['receiving'] = defaultdict(dict)
	txdb['pending'] = defaultdict(dict)
	txdb['pending']['receiving'] = defaultdict(dict)
	txdb['pending']['sending'] = defaultdict(dict)

# variable things =================

dispenserLowThreashold = 4
maxDeposit = 0	 # max dollars which can be inserted, this will change based on our wallet ballance
tickerPrice = 0 # global variable to store price in, needs to init as 0

# / variable things ===============

# SSP Stuff ================================

CashDrop = serial.Serial(port='/dev/ttyAMA0', baudrate=9600, timeout=1)
SSPrecycleChannel = 1 # 6 means recycle 100's
seqStateSSP = 0x00
channelValue        = [999, 1, 5, 10, 20, 50, 100, 0, 0, 0, 0, 0, 0, 0, 0, 0]
moneyCount = 0
lastSSPpollResponse = 0x00

decodeBufferSSP = array.array('B')

# SSP commands
SSP_sync				= [0x11]
SSP_enable_payout		= [0x5C, 0x00]
SSP_report_by_channel	= [0x45, 0x01]
SSP_enable				= [0x0A]
SSP_disable				= [0x09]
SSP_dispense			= [0x42]
SSP_poll				= [0x07]
SSP_get_positions		= [0x41]
SSP_inhibit_channels	= array.array('B', [0x02, 0x00, 0x00])  # controls which channels are enabled / disabled

# these variables are used when generating the deposit code.
dispenserJam    = False
stackerJam      = False
stackerFull     = False
fraudAttempt    = False  # someone tried to break in


# / SSP Stuff ================================

def main():
	getTxFee()
	processPendingTx()
	getPrices()
	to_cam.put('start')	# start the camera
	checkQR()
	
def getTxFee():
	try:
		tx_fee.TX_FEE_PER_THOUSAND_BYTES = int(json.load(urllib.urlopen('https://api.blockchain.info/fees'))['estimate'][0]['fee'])
	except:
		pass
	if tx_fee.TX_FEE_PER_THOUSAND_BYTES == 0:
		print "failed to get tx fee, trying again in 10 seconds"
		time.sleep(10)
		getTxFee()
		return
	else:
		getTxFee_thread = Timer(600, getPrices) # get getTxFee again in 120 seconds
		print "tx fee has been set to ", tx_fee.TX_FEE_PER_THOUSAND_BYTES
		getTxFee_thread.daemon = True
		getTxFee_thread.start()
	
	
# This is the constructor for the subpreocess which will control the camera and scan for QRs
def scanQR():
	# initialize camera interface
	camera = picamera.PiCamera()
	#
	# MAX_IMAGE_RESOLUTION required so preview field of view matches capture one.
	#camera.resolution = camera.MAX_IMAGE_RESOLUTION
	#
	# position and size info
	camera.rotation = 270
	camera.preview_fullscreen = False
	camera.preview_window = (10, 229, 323, 240)	# x, y, width, height
	#
	# loop forever
	while True:
		todo = to_cam.get()
		to_cam.task_done()
		if todo == 'start':
			# start writing camera image to screen
			camera.start_preview()
			#
			loop = True
			print "camera started"
			while loop:
				# Create an in-memory stream
				stream = io.BytesIO()
				#
				# jpeg to save memory, resize to make processing faster, use_video_port to not change modes
				camera.capture(stream, format='jpeg', resize=(640, 480), use_video_port=True)
				#
				# Rewind the stream for reading
				stream.seek(0)
				#
				# create a reader
				scanner = zbar.ImageScanner()
				#
				# configure the reader
				scanner.parse_config('enable')
				#
				# obtain image data
				pil = Image.open(stream).convert('L')
				width, height = pil.size
				raw = pil.tostring()
				#
				# wrap image data
				image = zbar.Image(width, height, 'Y800', raw)
				#
				# scan the image for barcodes
				scanner.scan(image)
				#
				# extract results, send them
				for symbol in image.symbols:
					if str(symbol.type) == 'QRCODE':
						if from_cam.empty():
							from_cam.put(str(symbol.data))
						camera.stop_preview()
						loop = False
						print "camera stopped by QR capture"
						break
				# stop if told
				if not to_cam.empty():
					todo = to_cam.get()
					if todo == 'stop':
						camera.stop_preview()
						loop = False
						print "camera stopped by command"
					to_cam.task_done()
					

					
					
def getPrices():
	global tickerPrice
	global maxDeposit
	global ourPrivateKey
	ourAddress = wif2address(ourPrivateKey)
	ourBalance = getBalance(ourAddress)
	if ourBalance == 0:
		print "failed to fetch our balance, retrying in 10 seconds"
		time.sleep(10)
		getPrices()
		return
	else:
		print "our balance is ", ourBalance, " BTC."
		ourBalance = ourBalance - 0.0001 # minus some fee
	tickerCoinbaseURL = "https://api.coinbase.com/v2/prices/BTC-USD/buy"
	try:
		tickerCoinbase = json.load(urllib.urlopen(tickerCoinbaseURL))
		tickerCoinbasePrice = round(float(tickerCoinbase["data"]["amount"]),2)
	except:
		tickerCoinbasePrice = 0
	print "Coinbase price: " , tickerCoinbasePrice
	tickerBitStampURL = "https://www.bitstamp.net/api/v2/ticker/btcusd"
	try:
		tickerBitStamp = json.load(urllib.urlopen(tickerBitStampURL))
		tickerBitStampPrice = round(float(tickerBitStamp["ask"]),2)
	except:
		tickerBitStampPrice = 0
	print "Bitstamp price: " , tickerBitStampPrice
	tickerBTCeURL = "https://btc-e.com/api/3/ticker/btc_usd"
	try:
		tickerBTCe = json.load(urllib.urlopen(tickerBTCeURL))
		tickerBTCePrice = round(float(tickerBTCe["btc_usd"]["buy"]),2)
	except:
		tickerBTCePrice = 0
	print "btc-e price:    " , tickerBTCePrice
	tickerPriceList = [tickerBitStampPrice, tickerCoinbasePrice, tickerBTCePrice]
	pendingTickerPrice = statistics.median(tickerPriceList)
	if pendingTickerPrice == 0:
		print "failed to fetch two or more price sources, retrying in 10 seconds"
		time.sleep(10)
		getPrices()
		return
	else:
		print "Got price. We are using:   " , pendingTickerPrice
		tickerPrice = pendingTickerPrice
		maxDeposit = int(tickerPrice * ourBalance)
		print "maxDeposit has been set to ", maxDeposit
		getPrices_thread = Timer(120, getPrices) # get prices again in 120 seconds
		getPrices_thread.daemon = True
		getPrices_thread.start()


def checkQR():
	if not from_cam.empty():				 # if we have a QR code...
		code = from_cam.get() 				 # get it
		processQRCode(code)					 # use it!
		from_cam.task_done()				 # allow from_cam.put() to return in the other process
	checkQR_thread = Timer(0.2, checkQR)     # check for one again later
	checkQR_thread.daemon = True
	checkQR_thread.start()
		
def processQRCode(code):
	print "The QR Code says " + code
	if is_valid_bitcoin_address(code):
		print "this is a bitcoin address"
		giveBTC(code)
		to_cam.put('start') # reset for the next person
		camera.start_preview()
	elif is_valid_wif(code):
		print "this is a private key"
		takeBTC(code)
		to_cam.put('start') # reset for the next person
	else:
		print "this is not a bitcoin address or private key"
		
def takeBTC(theirPrivateKey):
	global tickerPrice
	global moneyCount
	global ourPrivateKey
	global txdb
	theirAddress = wif2address(theirPrivateKey)
	addressBalance = getBalance(theirAddress)
	insertedDollars = round((addressBalance * tickerPrice),2)
	billValue = channelValue[SSPrecycleChannel]
	
	if theirAddress in txdb['receiving']:
		if txConfirmed(txdb['receiving'][theirAddress]['tx']):
			print "the tx has been confirmed"
			moneyCount = txdb['receiving'][theirAddress]['toDispenseBills']
			txdb.sync()
			if not SSPdispense():
				dollarsOwed = moneyCount * billValue
				bitcoinOwed = dollarsOwed / tickerPrice
				print "we were not able to dispense enough. we still owe $" , dollarsOwed
				print "sent " , bitcoinOwed , " BTC to " , theirAddress
				tx = processSendBitcoin(ourPrivateKey, theirAddress, bitcoinOwed)
				if tx:
					print "tx: ", tx
				else:
					txdb['pending']['sending'][theirAddress] = defaultdict(dict)
					txdb['pending']['sending'][theirAddress]['bitcoinOwed'] = bitcoinOwed
					txdb.sync()
					print "Couldn't process tx. Will try again in 120 seconds"
			else: 
				print "done. money dispensed"
			del txdb['receiving'][theirAddress]
			txdb.sync()
			return
		else:
			print "we are still waiting on a confirmation for a transaction for that address"
			return
	else:
		print "the corresponding address is: " + theirAddress
		print "the balance at that address is: " , addressBalance , " bitcoin"
		print "which is $" , insertedDollars
		if insertedDollars < billValue:
			print "doing nothing. minimum $" , billValue
			return
		toDispenseBills = math.trunc(insertedDollars / billValue)
		toDispenseValue = toDispenseBills * billValue
		toReturnDollars = round((insertedDollars - toDispenseValue),2)
		toTakeBitcoin = round(toDispenseValue / tickerPrice)
		toReturnBitcoin = round((toReturnDollars / tickerPrice),8)
		print "after confirmation, dispensing $" , toDispenseValue , " which is " , toDispenseBills , "bills and keeping " , toTakeBitcoin , " Bitcoin"
		print "sent " , toReturnBitcoin , " BTC to: " , theirAddress, " which is $" , toReturnDollars
		# make sure we are not taking more than they actually have (after standard tx fee applied)
		stdTxFee = (tx_fee.TX_FEE_PER_THOUSAND_BYTES / 100000000)
		if (toTakeBitcoin + stdTxFee) > addressBalance:
			toTakeBitcoin = addressBalance - stdTxFee
		processTakeBTC(theirPrivateKey, toTakeBitcoin, toDispenseBills)

def processTakeBTC(theirPrivateKey, toTakeBitcoin, toDispenseBills):
	global txdb
	global ourPrivateKey
	theirAddress = wif2address(theirPrivateKey)
	tx = processSendBitcoin(theirPrivateKey, wif2address(ourPrivateKey), toTakeBitcoin)
	if tx:
		txdb['receiving'][theirAddress] = {'tx':tx, 'toDispenseBills':toDispenseBills}
		txdb.sync()
		print "now waiting for confirmation"
	else:
		txdb['pending']['receiving'][theirAddress] = defaultdict(dict)
		txdb['pending']['receiving'][theirAddress]['theirPrivateKey'] = theirPrivateKey
		txdb['pending']['receiving'][theirAddress]['toTakeBitcoin'] = toTakeBitcoin
		txdb.sync()
		print "couldn't make the transaction right now, will try again in 120 seconds"
		
def processPendingTx():
	global txdb
	global ourPrivateKey
	for theirAddress, v in txdb['pending']['receiving'].items():
		ourAddress = wif2address(ourPrivateKey)
		print v['theirPrivateKey'] , '.' , ourAddress , '.' , v['toTakeBitcoin']
		tx = processSendBitcoin(v['theirPrivateKey'], ourAddress, v['toTakeBitcoin'])
		if tx:
			txdb['receiving'][theirAddress] = {'tx':tx, 'toDispenseBills':toDispenseBills}
			del txdb['pending']['receiving'][theirAddress]
			txdb.sync()
			print "tried to perform a failed receiving tx from earlier. it succeeded"
		else:
			print "tried to perform a failed receiving tx from earlier. it failed again"
			print "will try again in 120 seconds"
			
	for theirAddress, v in txdb['pending']['sending'].items():
		tx = processSendBitcoin(ourPrivateKey, theirAddress, v['bitcoinOwed'])
		if tx:
			del txdb['pending']['sending'][theirAddress]
			print "tried to perform a failed sending tx from earlier. it succeeded"
		else:
			print "tried to perform a failed sending tx from earlier. it failed again"
			print "will try again in 120 seconds"
	processPendingTx_thread = Timer(60, processPendingTx) # do this again later
	processPendingTx_thread.daemon = True
	processPendingTx_thread.start()

def processSendBitcoin(fromPrivateKey, toPublicKey, amountOfBitcoin):
	bcip = BlockchainInfoProvider("BTC")
	amountOfSatoshi = int(amountOfBitcoin * 100000000)
	spendables = spendables_for_address(wif2address(fromPrivateKey), "BTC")
	try:
		tx = create_signed_tx(spendables, [(toPublicKey, amountOfSatoshi), wif2address(fromPrivateKey)], [fromPrivateKey], tx_fee.TX_FEE_PER_THOUSAND_BYTES)
	except Exception as e:
		print "could not build transaction."
		print e
		return False
	try:
		bcip.broadcast_tx(tx)
		print "tx broadcasted: ", tx.id()
		return tx.id()
	except:
		print "tx failed to be broadcasted"
		return False

def txConfirmed(tx):
	blockchainAddressInfoURL = "https://blockchain.info/rawtx/" + tx + "?format=json"
	try:
		blockchainAddressInfo = json.load(urllib.urlopen(blockchainAddressInfoURL))
		if 'block_height' in blockchainAddressInfo:
			return True
	except:
		pass
	return False

def getBalance(address):
	blockchainAddressInfoURL = "https://blockchain.info/address/" + address + "?format=json"
	try:
		blockchainAddressInfo = json.load(urllib.urlopen(blockchainAddressInfoURL))
		addressBallance = float(blockchainAddressInfo['final_balance'])
		return round((addressBallance / 100000000),8)
	except Exception as e:
		print "error fetching balance:", e
		return 0
	

def giveBTC(address):
	global cashAcceptorOn
	global moneyCount
	global tickerPrice
	global ourPrivateKey
	cashAcceptorOn = True
	SSPsetup()
	setChannelInhibits()
	SSPcommunicate(SSP_enable)
	acceptCash()
	print "cash acceptor is on"
	time.sleep(2) # need to give them time to pull their phone away from the camera
	to_cam.put('start')
	try:
		from_cam.get(True, 90) # this is blocking for 90 seconds while we wait now for a QR code.
	except:
		print "cam timeout"
	cashAcceptorOn = False
	print "cash acceptor is off"
	if moneyCount == 0:
		print " no money inserted. canceling..."
	else:
		btcToSend = round((moneyCount / tickerPrice), 8)
		print "got $", moneyCount
		print "sending " , btcToSend , "bitcoins to: " , address
		processSendBitcoin(ourPrivateKey, address, btcToSend)
	time.sleep(2) # need to give them time to pull their phone away from the camera
	moneyCount = 0; # reset for the next person
			
		
def acceptCash():
	if cashAcceptorOn == True:
		acceptCash_thread = Timer(0.2, acceptCash) # loop this function again in a bit
		acceptCash_thread.daemon = True
		acceptCash_thread.start()
		SSPinterpret()
	else:
		SSPcommunicate(SSP_disable)
		
def wif2address(wif):
	private_key = Key.from_text(wif);
	return str(private_key.address())

		
# SSP functions =========================================
					
def culCalcCRC(crcData, crcReg):
	i = 0
	while i < 8:
		if ((crcReg & 0x8000) >> 8) ^ (crcData & 0x80):
			crcReg = (crcReg << 1) ^ 0x8005
		else:
			crcReg = (crcReg << 1)
		crcData <<= 1
		i += 1
	return (crcReg & 0x00ffff)


def SSPencode(command):
	encodeBufferSSP = array.array('B')
	length = len(command)
	checksum = 0xFFFF
	checksum = culCalcCRC(seqStateSSP, checksum)
	checksum = culCalcCRC(length, checksum)
	#
	encodeBufferSSP.append(0x7F) # first byte of SSP transmission is always the STX byte (not included in crc calculation)
	encodeBufferSSP.append(seqStateSSP)  # second byte is the sequence bit (and slave ID [0 in this case])
	encodeBufferSSP.append(length)  # 3rd byte is the length of the command data
	#
	i = 3
	while i < length + 3:
		encodeBufferSSP.append(command[i - 3])  # 4th through x bytes are the actucal command data
		checksum = culCalcCRC(command[i - 3], checksum)  # on which we continue to calcualte the crc
		i += 1
	encodeBufferSSP.append((checksum & 0x00ff))  # the last two bytes are the crc
	encodeBufferSSP.append(((checksum & 0xff00) >> 8))  # +3 and +4 because there are 3 bytes in front of the final array (STX, SEQ, Length)
	return encodeBufferSSP

def SSPcheck(decodeBufferSSP):
	if len(decodeBufferSSP):			# if there is any data at all
		if decodeBufferSSP[0] != 0x7F:
			return False
		else:							# if the init byte matches...
			checksum = 0xFFFF
			i = 1
			while i < decodeBufferSSP[2] + 3:
				checksum = culCalcCRC(decodeBufferSSP[i], checksum)
				i += 1
			try: # we can't trust decodeBufferSSP[2] to provide us a valid array index, so we plan for the worst.
				if decodeBufferSSP[decodeBufferSSP[2] + 3] != (checksum & 0x00ff):
					return False
				else:
					if decodeBufferSSP[decodeBufferSSP[2] + 4] != ((checksum & 0xff00) >> 8):
						return False
					else:
						return True
			except:
				return False

def SSPreceive():
	global decodeBufferSSP
	decodeBufferSSP = array.array('B')
	while CashDrop.inWaiting() > 0:
		decodeBufferSSP.append(ord(CashDrop.read(1)))

def SSPcommunicate(command):
	global seqStateSSP
	global decodeBufferSSP
	while True:
		CashDrop.write(SSPencode(command).tostring())  # encodes an SSP command then sends it over serial
		time.sleep(0.1)
		SSPreceive()
		if SSPcheck(decodeBufferSSP):  # keeps sending until the response makes sense
			break
	if decodeBufferSSP[1] == 0x00:
		seqStateSSP = 0x80
	if decodeBufferSSP[1] == 0x80:
		seqStateSSP = 0x00

def SSPsync():
	global seqStateSSP
	SSPcommunicate(SSP_sync)
	seqStateSSP = 0x00

def SSPrecycle(channel):
	buffer = array.array('B')
	buffer.append(0x3B)  # the command
	buffer.append(0x00)  # the route: route 0 = recycler, 1 = stacker
	buffer.append(channel)   # the channel number
	SSPcommunicate(buffer)

def SSPsetup():
	SSPsync()
	SSPcommunicate(SSP_enable_payout)
	SSPcommunicate(SSP_report_by_channel)
	SSPrecycle(SSPrecycleChannel)

def bitSet(x, n):
	y = 1 << n
	x = x ^ y
	return x

def setChannelInhibits():  		# enables all channels which do not have a zero value
	global SSP_inhibit_channels
	channelInhibits = 0x0000  	# disable all channels
	i = 1
	while i < 16:
		if channelValue[i] > 0:          # channel with nonzero value...
			if channelValue[i] + moneyCount < maxDeposit:  # which would not push over the maxDeposit
				channelInhibits = bitSet(channelInhibits, i - 1)  			# enable it
		i += 1
	SSP_inhibit_channels[1] = (channelInhibits & 0x00ff)  	# write the data back to the array
	SSP_inhibit_channels[2] = (channelInhibits & 0xff00)   	# write the data back to the array
	SSPcommunicate(SSP_inhibit_channels)  # communicate it to the device


def SSPinterpret():
	global lastSSPpollResponse
	global moneyCount
	global stackerJam
	global dispenserJam
	global stackerFull
	global fraudAttempt
	global decodeBufferSSP
	SSPcommunicate(SSP_poll)
	lastSSPpollResponse = decodeBufferSSP[4]
	#print "lastSSPpollResponse: ", lastSSPpollResponse
	if (decodeBufferSSP[4] == 0xEE):   # 0xEE means they put money in and it was accepted
		moneyCount += channelValue[decodeBufferSSP[5]]  # byte 5 has the channel number of accepted bill
		setChannelInhibits() # disable channels which would allow the user to go over maxDeposit
		stackerJam = False  # if they put in money, it must not be jammed (anymore?)

  
	if (decodeBufferSSP[4] == 0xD2):   # 0xD2 means dispensed
		dispenserJam = False     # if they took a bill, it must not be jammed (anymore?)
  
	if (decodeBufferSSP[4] == 0xEA):   # 0xEA means acceptor jam, but they can't get to the money
		moneyCount += channelValue[decodeBufferSSP[5]]  # byte 5 has the channel number of accepted bill
		setChannelInhibits() # disable channels which would allow the user to go over maxDeposit
		stackerJam = True
  
	if (decodeBufferSSP[4] == 0xE9):   # 0xE9 also means acceptor jam, but the money it hanging out
		stackerJam = True
  
	if (decodeBufferSSP[4] == 0xD5):   # 0xD5 means dispensor jam
		dispenserJam = True
  
	if (decodeBufferSSP[4] == 0xE7):   # 0xE7 means stacker full
		stackerFull = True
  
	if (decodeBufferSSP[4] == 0xE6):   # 0xE6 someone tried to break in
		fraudAttempt = True
	return decodeBufferSSP

def cashOnHand():  # returns the number of bills in the cash dispenser
	SSPcommunicate(SSP_get_positions)
	return decodeBufferSSP[4]
    
def SSPdispense():
	global moneyCount
	global decodeBufferSSP
	SSPcommunicate(SSP_enable_payout)  	# in case the payout device had gone out of service (from a jam)
	while moneyCount > 0:   			# loop 'moneyCount' times, (despense that many bills)
		if not cashOnHand():
			return False     				# if money runs out, return false
		SSPcommunicate(SSP_dispense)  		# dispense
		while True:   						# loop while they have not taken the bill 
			time.sleep(.2)
			SSPinterpret()  		# polls every so often
			if lastSSPpollResponse == 0xD2:   		# 0xD2 means dispensed
				moneyCount -= 1		# we owe them a little less now
				while lastSSPpollResponse == 0xD2:   # wait until it no longer shows dispensed (next pass of the poll)
					time.sleep(.2)
					SSPinterpret()
				break
	return True

# start the subprocess, main loop, and gui
if __name__ == '__main__':
	main()
	multiprocessing.Process(target=scanQR, args=()).start()
