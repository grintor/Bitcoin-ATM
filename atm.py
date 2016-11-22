import picamera
import multiprocessing
import io
import zbar
from PIL import Image
from threading import Timer
from hashlib import sha256
from pycoin.encoding import is_valid_wif, public_pair_to_bitcoin_address, is_valid_bitcoin_address, wif_to_secret_exponent
from pycoin.ecdsa import generator_secp256k1, public_pair_for_secret_exponent

import urllib
import json
import statistics
import serial
import array
import time

to_cam = multiprocessing.JoinableQueue(1)
from_cam = multiprocessing.JoinableQueue(1)

cashAcceptorOn = False # global variable to tell acceptCash() if it should keep looping or not

# variable things =================

moneyDiv = 5
secretKey = "BitcoinsFTW!"
orxKey = "abc1234"
machineID = 333
dispenserLowThreashold = 4
maxDeposit = 1200	 # max dollars which can be inserted, must be smaller than 256*moneyDiv
tickerPrice = 0 # global variable to store price in, needs to init as 0

# / variable things ===============

# SSP Stuff ================================

CashDrop = serial.Serial(port='/dev/ttyAMA0', baudrate=9600, timeout=1)
SSPrecycleChannel = 6 # 6 means recycle 100's
seqStateSSP = 0x00
channelValue        = [999, 0, 5, 10, 20, 50, 100, 0, 0, 0, 0, 0, 0, 0, 0, 0]
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
	global tickerPrice
	getPrices()
	to_cam.put('start')	# start the camera
	checkQR()
	
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
						break
				# stop if told
				if not to_cam.empty():
					todo = to_cam.get()
					if todo == 'stop':
						camera.stop_preview()
						loop = False
						print "camera stopped"
					to_cam.task_done()
					

					
					
def getPrices():
	global tickerPrice
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
	tickerPrice = statistics.median(tickerPriceList)
	if tickerPrice == 0:
		print "failed to fetch two or more price sources, retrying in 10 seconds"
		time.sleep(10)
		getPrices()
	else:
		print "Got price. We are using:   " , tickerPrice
		getPrices_thread = Timer(90, getPrices) # get prices again in 90 seconds
		getPrices_thread.start()


def checkQR():
	if not from_cam.empty():				# if we have a QR code...
		code = from_cam.get() 				# get it
		processQRCode(code)					# use it!
		from_cam.task_done()				# allow from_cam.put() to return in the other process
	else:
		checkQR_thread = Timer(0.2, checkQR) # if we don't have one, check for one later
		checkQR_thread.start()
		
def processQRCode(code):
	print "The QR Code says " + code
	if is_valid_bitcoin_address(code):
		print "this is a bitcoin address"
		giveBTC(code)
	elif is_valid_wif(code):
		print "this is a private key"
		takeBTC(code)
	else:
		print "this is not a bitcoin address or private key"
		
def takeBTC(privateKey):
	global tickerPrice
	address = wif2address(privateKey)
	balance = getBalance(wif2address(privateKey))
	dollars = round((balance * tickerPrice),2)
	billValue = channelValue[SSPrecycleChannel]
	print "the corrasponding address is: " + address
	print "the balance at that address is: " , balance , " bitcoin"
	print "which is $" , dollars
	if dollars < billValue:
		print "doing nothing. minimum $" , billValue
	else:
		toDispenseBills = math.trunc(dollars / billValue)
		toDispenseValue = toDispenseBills * billValue
		toReturnDollars = round((dollars - toDispenseValue),2)
		toReturnBitcoin = round((toReturnDollars / tickerPrice),8)
		print "dispensing $" , toDispenseValue , " which is " , toDispenseBills , "bills"
		print "sending $" , toReturnDollars , " which is " , toReturnBitcoin , " BTC to: " , address
		
	
def getBalance(address):
	blockchainAddressInfoURL = "https://blockchain.info/address/" + address + "?format=json"
	try:
		blockchainAddressInfo = json.load(urllib.urlopen(blockchainAddressInfoURL))
		addressBallance = blockchainAddressInfo["final_balance"]
		return addressBallance / 100000000
	except:
		return 0
	

def giveBTC(address):
	global cashAcceptorOn
	global moneyCount
	global tickerPrice
	cashAcceptorOn = True
	SSPsetup()
	setChannelInhibits()
	SSPcommunicate(SSP_enable)
	acceptCash()
	print "cash acceptor is on"
	time.sleep(2) # need to give them time to pull their phone away fromt the camera
	to_cam.put('start')
	try:
		from_cam.get(True, 90) # this is blocking for 90 seconds while we wait now for a QR code.
	except:
		print "cam timeout"
	cashAcceptorOn = False
	print "cash acceptor is off"
	if moneyCount == 0:
		print "canceling..."
	else:
		btcToSend = round((moneyCount / tickerPrice), 8)
		print "got $", moneyCount
		print "sending " , btcToSend , "bitcoins to: " , address
	time.sleep(2) # need to give them time to pull their phone away from the camera
	moneyCount = 0; # reset for the next person
	to_cam.put('start') # reset for the next person
			
		
def acceptCash():
	if cashAcceptorOn == True:
		acceptCash_thread = Timer(0.2, acceptCash) # loop this function again in a bit
		acceptCash_thread.start()
		SSPinterpret()
	else:
		SSPcommunicate(SSP_disable)
		
def wif2address(wif):
	secret_exponent = wif_to_secret_exponent(wif)
	public_pair = public_pair_for_secret_exponent(generator_secp256k1, secret_exponent)
	return public_pair_to_bitcoin_address(public_pair, False)

		
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
			if decodeBufferSSP[decodeBufferSSP[2] + 3] != (checksum & 0x00ff):
				return False
			else:
				if decodeBufferSSP[decodeBufferSSP[2] + 4] != ((checksum & 0xff00) >> 8):
					return False
				else:
					return True

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
