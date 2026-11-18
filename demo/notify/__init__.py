"""Notification demo: queue messages, render them, and deliver them through a gateway.

Sizes the service is built for: a nightly batch holds up to 200 000 notifications,
a user has a handful of preferences, and the gateway is a remote HTTP service that
times out now and then.
"""
