import logging
import json
import time
import curses.ascii
from threading import Thread, Lock
from queue import Queue
from collections import namedtuple
import serial

from odoo import http

from odoo.addons.hw_drivers.controllers.proxy import proxy_drivers
from odoo.addons.hw_drivers.event_manager import event_manager
from odoo.addons.hw_drivers.iot_handlers.drivers.SerialBaseDriver import SerialDriver, SerialProtocol, serial_connection

# Only needed to ensure compatibility with older versions of Odoo
ACTIVE_TERMINAL = None

_logger = logging.getLogger(__name__)

try:
    import pycountry

    EUR_CY_NBR = False
except (ImportError, IOError) as err:
    _logger.debug(err)
    _logger.warning(
        'Unable to import pycountry, only EUR currency is supported')
    EUR_CY_NBR = 978

TeliumProtocol = namedtuple('TeliumProtocol',
                            SerialProtocol._fields + ('transaction_msg_length', 'response_msg_length'))

TeliumEplusProtocol = TeliumProtocol(
    name='Telium E+',
    baudrate=9600,
    bytesize=serial.EIGHTBITS,
    stopbits=serial.STOPBITS_ONE,
    parity=serial.PARITY_NONE,
    timeout=3,  # Don't modify timeout=3 seconds. This parameter is very important ; the Telium spec say
    # that we have to wait to up 3 seconds to get LRC
    writeTimeout=1,

    commandDelay=3,
    measureDelay=5,
    newMeasureDelay=5,

    measureRegexp=None,
    statusRegexp=None,
    commandTerminator=None,
    measureCommand=None,
    emptyAnswerValid=None,

    transaction_msg_length=34,
    response_msg_length=1 + 2 + 1 + 8 + 1 + 3 + 10 + 1 + 1,
)


# Ensures compatibility with older versions of Odoo
class TeliumPaymentTerminalControllerOldRoute(http.Controller):
    @http.route(
        '/hw_proxy/payment_terminal_transaction_start',
        type='json', auth='none', cors='*')
    def payment_terminal_transaction_start(self, payment_info):
        _logger.debug(
            'Telium: Call payment_terminal_transaction_start with '
            'payment_info=%s', payment_info)
        if ACTIVE_TERMINAL:
            return ACTIVE_TERMINAL._start_transaction_old_route(payment_info)
        return None


class TeliumPaymentTerminalDriver(SerialDriver):
    _protocol = TeliumEplusProtocol

    def __init__(self, identifier, device):
        super(TeliumPaymentTerminalDriver, self).__init__(identifier, device)
        self.device_type = 'telium_payment_terminal'
        self._set_actions()

        self.queue = Queue()

        global ACTIVE_TERMINAL
        ACTIVE_TERMINAL = self
        proxy_drivers['telium_payment_terminal'] = ACTIVE_TERMINAL

    # Ensures compatibility with older versions of Odoo
    # and allows using the `ProxyDevice` in the point of sale to retrieve the status
    def get_status(self):
        """Allows `hw_proxy.Proxy` to retrieve the status of the terminal"""

        status = self._status
        return {'status': status['status'], 'messages': [status['message_title'], ]}

    def _set_actions(self):
        """Initializes `self._actions`, a map of action keys sent by the frontend to backend action methods."""

        self._actions.update({
            'payment_terminal_transaction_start': self._transaction_start,
        })

    @staticmethod
    def send_one_byte_signal(connection, signal):
        ascii_names = curses.ascii.controlnames
        assert signal in ascii_names, 'Wrong signal'
        char = ascii_names.index(signal)
        raw = chr(char).encode()
        connection.write(raw)
        _logger.debug('Signal %s sent to terminal' % signal)

    @staticmethod
    def get_one_byte_answer(connection, expected_signal):
        assert isinstance(expected_signal, str), 'expected_signal must be a string'
        ascii_names = curses.ascii.controlnames
        raw = connection.read(1)
        one_byte_read = raw.decode('ascii')
        expected_char = ascii_names.index(expected_signal)
        if one_byte_read == chr(expected_char):
            return True
        else:
            return False

    @classmethod
    def supported(cls, device):
        """Checks whether the device, which port info is passed as argument, is supported by the driver.

        :param device: path to the device
        :type device: str
        :return: whether the device is supported by the driver
        :rtype: bool
        """
        protocol = cls._protocol
        try:
            with serial_connection(device['identifier'], protocol, is_probing=False) as connection:
                max_attempt = 3
                attempt_nr = 0
                while attempt_nr < max_attempt:
                    attempt_nr += 1
                    cls.send_one_byte_signal(connection, 'ENQ')
                    if cls.get_one_byte_answer(connection, 'ACK'):
                        return True
                    else:
                        _logger.warning("Probing Terminal %d/%d: TRY AGAIN" % (attempt_nr, max_attempt))
                        cls.send_one_byte_signal(connection, 'EOT')
                        # Wait 1 sec between each attempt
                        time.sleep(1)  #
                return False
        except serial.serialutil.SerialTimeoutException:
            pass
        except Exception:
            _logger.exception('Error while probing %s with protocol %s' % (device, protocol.name))
        return False

    # Ensures compatibility with older versions of Odoo
    def _start_transaction_old_route(self, payment_info):
        """Used when the iot app is not installed"""
        with self._device_lock:
            self._transaction_start(payment_info)

    def _set_status(self, status, message=None):
        if status == self._status['status']:
            if message is not None and message != self._status['messages'][-1]:
                self._status['messages'].append(message)
        else:
            self._status['status'] = status
            if message:
                self._status['messages'] = [message]
            else:
                self._status['messages'] = []

        if status == 'error' and message:
            _logger.error('Payment Terminal Error: ' + message)
        elif status == 'disconnected' and message:
            _logger.warning('Disconnected Terminal: ' + message)

    def _serial_write(self, text):
        assert isinstance(text, str), 'text must be a string'
        raw = text.encode()
        _logger.debug("%s raw send to terminal" % raw)
        _logger.debug("%s send to terminal" % text)
        self._connection.write(raw)

    def _serial_read(self, size=1):
        raw = self._connection.read(size)
        msg = raw.decode('ascii')
        _logger.debug("%s raw received from terminal" % raw)
        _logger.debug("%s received from terminal" % msg)
        return msg

    def _initialize_msg(self):
        max_attempt = 3
        attempt_nr = 0
        while attempt_nr < max_attempt:
            attempt_nr += 1
            self._send_one_byte_signal('ENQ')
            if self._get_one_byte_answer('ACK'):
                return True
            else:
                _logger.warning("Terminal : SAME PLAYER TRY AGAIN")
                self._send_one_byte_signal('EOT')
                # Wait 1 sec between each attempt
                time.sleep(1)
        return False

    def _send_one_byte_signal(self, signal):
        ascii_names = curses.ascii.controlnames
        assert signal in ascii_names, 'Wrong signal'
        char = ascii_names.index(signal)
        self._serial_write(chr(char))
        _logger.debug('Signal %s sent to terminal' % signal)

    def _get_one_byte_answer(self, expected_signal):
        assert isinstance(expected_signal, str), 'expected_signal must be a string'
        ascii_names = curses.ascii.controlnames
        one_byte_read = self._serial_read(1)
        expected_char = ascii_names.index(expected_signal)
        if one_byte_read == chr(expected_char):
            return True
        else:
            return False

    def _get_amount(self, payment_info_dict):
        amount = payment_info_dict['amount']
        cur_decimals = payment_info_dict['currency_decimals']
        cur_fact = 10 ** cur_decimals
        return ('%.0f' % (amount * cur_fact)).zfill(8)

    def _prepare_data_to_send(self, payment_info_dict):
        if payment_info_dict['payment_mode'] == 'check':
            payment_mode = 'C'
        elif payment_info_dict['payment_mode'] == 'card':
            payment_mode = '1'
        else:
            _logger.error(
                "The payment mode '%s' is not supported"
                % payment_info_dict['payment_mode'])
            return False

        cur_iso_letter = payment_info_dict['currency_iso'].upper()
        try:
            if EUR_CY_NBR:
                cur_numeric = str(EUR_CY_NBR)
            else:
                cur = pycountry.currencies.get(alpha_3=cur_iso_letter)
                cur_numeric = str(cur.numeric)
        except:
            _logger.error("Currency %s is not recognized" % cur_iso_letter)
            return False
        data = {
            'pos_number': str(1).zfill(2),
            'answer_flag': '0',
            'transaction_type': '0',
            'payment_mode': payment_mode,
            'currency_numeric': cur_numeric.zfill(3),
            'private': ' ' * 10,
            'delay': 'A010',
            'auto': 'B010',
            'amount_msg': self._get_amount(payment_info_dict),
        }
        return data

    def _generate_lrc(self, real_msg_with_etx):
        lrc = 0
        for char in real_msg_with_etx:
            lrc ^= ord(char)
        return lrc

    def _send_message(self, data):
        '''We use protocol E+'''
        ascii_names = curses.ascii.controlnames
        real_msg = (
            data['pos_number'] +
            data['amount_msg'] +
            data['answer_flag'] +
            data['payment_mode'] +
            data['transaction_type'] +
            data['currency_numeric'] +
            data['private'] +
            data['delay'] +
            data['auto'])
        _logger.debug('Real message to send = %s' % real_msg)
        assert len(real_msg) == self._protocol.transaction_msg_length, 'Wrong length for protocol E+'
        real_msg_with_etx = real_msg + chr(ascii_names.index('ETX'))
        lrc = self._generate_lrc(real_msg_with_etx)
        message = chr(ascii_names.index('STX')) + real_msg_with_etx + chr(lrc)
        self._serial_write(message)
        _logger.info('Message sent to terminal')

    def _compare_data_vs_answer(self, data, answer_data):
        for field in ['pos_number', 'amount_msg', 'currency_numeric', 'private']:
            if data[field] != answer_data[field]:
                _logger.warning(
                    "Field %s has value '%s' in data and value '%s' in answer"
                    % (field, data[field], answer_data[field]))

    def _parse_terminal_answer(self, real_msg, data):
        answer_data = {
            'pos_number': real_msg[0:2],
            'transaction_result': real_msg[2],
            'amount_msg': real_msg[3:11],
            'payment_mode': real_msg[11],
            'currency_numeric': real_msg[12:15],
            'private': real_msg[15:26],
        }
        _logger.debug('answer_data = %s' % answer_data)
        self.compare_data_vs_answer(data, answer_data)
        return answer_data

    def _get_answer_from_terminal(self, data):
        ascii_names = curses.ascii.controlnames
        msg = self._serial_read(size=self._protocol.response_msg_length)
        _logger.debug('%d bytes read from terminal' % self._protocol.response_msg_length)
        assert len(msg) == self._protocol.response_msg_length, 'Answer has a wrong size'
        if msg[0] != chr(ascii_names.index('STX')):
            _logger.error(
                'The first byte of the answer from terminal should be STX')
        if msg[-2] != chr(ascii_names.index('ETX')):
            _logger.error(
                'The byte before final of the answer from terminal '
                'should be ETX')
        lrc = msg[-1]
        computed_lrc = chr(self._generate_lrc(msg[1:-1]))
        if computed_lrc != lrc:
            _logger.error(
                'The LRC of the answer from terminal is wrong')
        real_msg = msg[1:-2]
        _logger.debug('Real answer received = %s' % real_msg)
        return self._parse_terminal_answer(real_msg, data)

    def _transaction_start(self, payment_info):
        '''This function sends the data to the serial/usb port.
        '''
        payment_info_dict = json.loads(payment_info)
        assert isinstance(payment_info_dict, dict), \
            'payment_info_dict should be a dict'
        res = False
        try:
            if self._initialize_msg():
                data = self._prepare_data_to_send(payment_info_dict)
                if not data:
                    return res
                self._send_message(data)
                if self._get_one_byte_answer('ACK'):
                    self._send_one_byte_signal('EOT')
                    res = True

                    # TODO Thilo: This status is needed for pos_payment_terminal. Check if it set/used correctly
                    self._status['in_transaction'] = True
                    _logger.debug("Now expecting answer from Terminal")

                    # We wait the end of transaction
                    attempt_nr = 0
                    while attempt_nr < 600:
                        attempt_nr += 1
                        if self._get_one_byte_answer('ENQ'):
                            self._send_one_byte_signal('ACK')
                            answer = self._get_answer_from_terminal(data)
                            # '0' : accepted transaction
                            # '7' : refused transaction
                            if answer['transaction_result'] == '0' \
                                and self._get_amount(payment_info_dict) == answer['amount_msg']:
                                self._status['latest_transactions'] = {payment_info_dict['order_id']: {}}
                                _logger.info("Transaction OK")
                            self._send_one_byte_signal('ACK')
                            if self._get_one_byte_answer('EOT'):
                                _logger.debug("Answer received from Terminal")
                            break
                        time.sleep(0.5)
                    self._status['in_transaction'] = False

        except Exception as e:
            _logger.error('Exception in serial connection: %s' % str(e))
            # self.set_status("error",
            #                 "Exception in serial connection to {}"
            #                 .format(self.device_name))
        return res

    # def run(self):
    #     while True:
    #         try:
    #             timestamp, task, data = self.queue.get(True)
    #             if task == 'transaction_start':
    #                 self.transaction_start(data)
    #             elif task == 'status':
    #                 pass
    #         except Exception as e:
    #             self.set_status('error', str(e))
