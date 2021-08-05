import logging
import json
import time
import curses.ascii
from threading import Lock
from queue import Queue
import traceback
from usb import util

from odoo import http

from odoo.addons.hw_drivers.controllers.proxy import proxy_drivers
from odoo.addons.hw_drivers.event_manager import event_manager
from odoo.addons.hw_drivers.driver import Driver

from ecrterm.ecr import ECR
from ecrterm.packets.base_packets import Registration

#For transport debug logging
from ecrterm.common import TERMINAL_STATUS_CODES
from ecrterm.conv import bs2hl, toBytes, toHexString
from ecrterm.packets.base_packets import (
    Authorisation, Completion, DisplayText, EndOfDay, Packet, PrintLine,
    Registration, ResetTerminal, StatusEnquiry, StatusInformation)
from ecrterm.utils import is_stringlike
from ecrterm.ecr import parse_represented_data

# Only needed to ensure compatibility with older versions of Odoo
ACTIVE_TERMINAL = None

_logger = logging.getLogger(__name__)

# Remove later
try:
    import pycountry

    EUR_CY_NBR = False
except (ImportError, IOError) as err:
    _logger.debug(err)
    _logger.warning(
        'Unable to import pycountry, only EUR currency is supported')
    EUR_CY_NBR = 978


def ecr_log(data, incoming=False):
    """Logger stub for the ecrterm transport layer log"""
    try:
        if incoming:
            incoming = '<'
        else:
            incoming = '>'
        if is_stringlike(data):
            data = bs2hl(data)
        try:
            _logger.debug('%s %s\n' % (incoming, toHexString(data)))
        except Exception:
            pass
        try:
            data = repr(parse_represented_data(data))
            _logger.debug('= %s\n' % data)
        except Exception as e:
            _logger.debug('Cannot be represented: %s, %s' % (data, e))
            data = toHexString(data)
        _logger.debug('%s %s' % (incoming, data))
    except Exception:
        _logger.debug("Error in log: %s" % traceback.format_exc())

# Ensures compatibility with older versions of Odoo
class ZVT700PaymentTerminalControllerOldRoute(http.Controller):
    @http.route(
        '/hw_proxy/payment_terminal_transaction_start',
        type='json', auth='none', cors='*')
    def payment_terminal_transaction_start(self, payment_info):
        _logger.debug(
            'Call payment_terminal_transaction_start via old route with '
            'payment_info=%s' % (payment_info))
        if ACTIVE_TERMINAL:
            return ACTIVE_TERMINAL._start_transaction_old_route(payment_info)
        return None


class ZVT700PaymentTerminalDriver(Driver):
    connection_type = 'serial'
    protocol_name = 'ZVT-700'
    priority = 1

    def __init__(self, identifier, device):
        super(ZVT700PaymentTerminalDriver, self).__init__(identifier, device)
        self.device_type = 'payment_terminal'
        self._actions = {
            'get_status': self._push_status,
            'payment_terminal_transaction_start': self._transaction_start,
        }
        self.device_name = self._set_name()

        self.queue = Queue()
        self._device_lock = Lock()

        self.device_connection = 'serial'

        if self._connect_and_configure(self.device_identifier):
            status = 'connected'
        else:
            status = 'disconnected'

        self._status = {'status': status, 'message_title': '', 'message_body': ''}
        self._status['in_transaction'] = False

        global ACTIVE_TERMINAL
        ACTIVE_TERMINAL = self
        proxy_drivers['payment_terminal'] = ACTIVE_TERMINAL

    def _set_name(self):
        try:
            #Requires SerialInterface.py to search for '/dev/serial/by-id/*' instead of '/dev/serial/by-path/*'
            id_parts = self.device_identifier.split('/')[-1].split('-')[1].split('_')
            manufacturer = id_parts[0]
            product = id_parts[1]
            return ("%s - %s") % (manufacturer, product)
        except IndexError or ValueError as e:
            _logger.warning(e)
            return ('Unknown %s device' % self.protocol_name)

    def _connect_and_configure(self, device):
        self._ecr = ECR(device=device, password='111111')
        #Enable transport layer logging
        #self._ecr.transport.slog = ecr_log

        if self._ecr.detect_pt():
            _logger.debug("Configuring %s terminal (%s)" % (self.protocol_name, self.device_identifier))
            self._ecr.register(config_byte=Registration.generate_config(
                ecr_prints_receipt=True,
                ecr_prints_admin_receipt=True,
                ecr_controls_admin=True,
                ecr_controls_payment=True))

            self._ecr.wait_for_status()
            status = self._ecr.status()
            _logger.debug("%s terminal status is: %s" % (self.protocol_name, hex(status)))
            return True
        else:
            _logger.debug("Detecting %s terminal (%s) failed" % (self.protocol_name, self.device_identifier))

        return False

    # Ensures compatibility with older versions of Odoo
    # and allows using the `ProxyDevice` in the point of sale to retrieve the status
    def get_status(self):
        """Allows `hw_proxy.Proxy` to retrieve the status of the terminal"""
        _logger.debug("Old 'get_status()' method called")
        status = self._status
        return {'status': status['status'], 'messages': [status['message_title'], ]}

    def _push_status(self):
        """Updates the current status and pushes it to the frontend."""

        self.data['status'] = self._status
        event_manager.device_changed(self)

    @classmethod
    def supported(cls, device):
        """Checks whether the device, which port info is passed as argument, is supported by the driver.

        :param device: path to the device
        :type device: str
        :return: whether the device is supported by the driver
        :rtype: bool
        """
        try:
            _logger.debug("Probing device %s with %s protocol" % (device['identifier'], cls.protocol_name))
            e = ECR(device=device['identifier'], password='111111')
            #Enable transport layer logging
            #e.transport.slog = ecr_log

            if e.detect_pt():
                return True
            else:
                _logger.debug("Device %s does not comply with %s protocol" % (device['identifier'], cls.protocol_name))
        except Exception:
            _logger.debug('Error while probing %s with %s protocol' % (device, cls.protocol_name))
            pass
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

    def _get_amount(self, payment_info_dict):
        amount = payment_info_dict['amount']
        cur_decimals = payment_info_dict['currency_decimals']
        cur_fact = 10 ** cur_decimals
        return amount * cur_fact

    def _get_currency_code(self, payment_info_dict):
        cur_iso_letter = payment_info_dict['currency_iso'].upper()
        try:
            if EUR_CY_NBR:
                cur_numeric = str(EUR_CY_NBR)
            else:
                cur = pycountry.currencies.get(alpha_3=cur_iso_letter)
                cur_numeric = str(cur.numeric)
        except:
            _logger.error("Currency %s is not recognized" % cur_iso_letter)
        return cur_numeric

    def _get_payment_type(self, payment_info_dict):
        if payment_info_dict['payment_mode'] == 'check':
            payment_mode = 'C'
        elif payment_info_dict['payment_mode'] == 'card':
            payment_mode = '1'
        else:
            _logger.error(
                "The payment mode '%s' is not supported"
                % payment_info_dict['payment_mode'])
            return False

    def _terminal_wait_for_status(self, poll_interval=0.5, timeout=5):
        """
        waits until self.status() returns 0 (or False/None)
        or a timeout occurs; wait poll_interval seconds between attempts
        """
        attempt_ctr = timeout // poll_interval
        status = self._ecr.status()
        while status and attempt_ctr > 0:
            if self._ecr.transport.insert_delays:
                time.sleep(poll_interval)
            attempt_ctr += 1
            status = self._ecr.status()
        return status

    def _transaction_start(self, payment_info):
        """This function executes the transaction
        """
        # Examples
        # payment_info={"amount":750, "currency_iso": "EUR", "currency_decimals": 2, "payment_mode": "card", "order_id": "00014-001-0001"}
        # payment_info={'amount': 0.5, 'currency_iso': 'EUR', 'currency_decimals': 2, 'payment_mode': 'card', 'order_id': '00014-001-0001'}
        payment_info_dict = json.loads(payment_info)
        assert isinstance(payment_info_dict, dict), \
            'payment_info_dict should be a dict'
        res = False

        _logger.debug("Starting %s transaction: %s" % (self.protocol_name, payment_info_dict))
        self._status['in_transaction'] = True
        self._push_status()
        #time.sleep (3)

        amount = self._get_amount(payment_info_dict)

        # Currently not used
        currency_code = self._get_currency_code(payment_info_dict)
        payment_type = self._get_payment_type(payment_info_dict)

        try:
            if self._ecr.payment(amount_cent=amount):
                printout = self._ecr.last_printout()
                _logger.debug("Last printout: %s" % printout)
                status = self._terminal_wait_for_status(poll_interval=0.5, timeout=5)
                _logger.debug("Status after transaction: %s" % status)

                self._status['latest_transactions'] = {payment_info_dict['order_id']: {printout}}
                self._push_status()

                self._ecr.show_text(
                    lines=['Auf Wiedersehen!', ' ', 'Zahlung erfolgt'], beeps=0)
                res = True
            else:
                self._ecr.show_text(
                    lines=['Zahlung fehlgeschlagen', 'oder abgebrochen.', ' '], beeps=0)
        except Exception as e:
            _logger.error('Exception during terminal transaction: %s' % str(e))
            self.set_status("error",
                             "Exception during terminal transaction {}"
                             .format(self.device_name))
        finally:
            self._status['in_transaction'] = False
            self._push_status()
        return res

    def _do_action(self, data):
        """Helper function that calls a specific action method on the device.

        :param data: the `_actions` key mapped to the action method we want to call
        :type data: string
        """
        try:
            with self._device_lock:
                self._actions[data['action']](data)
        except Exception:
            msg = 'An error occured while performing action %s on %s' % (data, self.device_name)
            _logger.exception(msg)
            self._status = {'status': 'error', 'message_title': msg,
                            'message_body': traceback.format_exc()}
            self._push_status()

    def action(self, data):
        """Establish a connection with the device if needed and have it perform a specific action.

        :param data: the `_actions` key mapped to the action method we want to call
        :type data: string
        """
        if self._ecr.wait_for_status() == 0:  # No error
            self._do_action(data)
        else:
            self._connect_and_configure(self.device_identifier)
            self._do_action(data)
