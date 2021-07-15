Hardware drivers
===============

This folder follows the directory structure of [odoo/addons/hw_drivers](https://github.com/odoo/odoo/tree/14.0/addons/hw_drivers) of the `default` branch, since the iotbox is built from the most recent version of odoo.

Test scripts reside in the [tools](tools/) folder.

Available drivers
----------------
driver | additional files | version | summary
--- | --- | --- | ---
[TeliumPaymentTerminalDriver.py](iot_handlers/drivers/TeliumPaymentTerminalDriver.py) | [telium_payment_terminal](iot_handlers/drivers/telium_payment_terminal/)  | | Adds support for Payment Terminals using Telium protocol
