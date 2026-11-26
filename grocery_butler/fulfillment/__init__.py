"""Fulfillment adapters: authorized, replaceable ways to hand a plan off.

ADR 0001: fulfillment goes through adapters behind a retailer-neutral
interface. This package holds that interface (:mod:`.base`), the registry
(:mod:`.registry`) and the adapters. Core domain modules never import a
concrete adapter; they depend on :mod:`.base` at most.
"""
