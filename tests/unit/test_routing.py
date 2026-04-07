"""Unit tests for call routing."""

from app.services.routing import determine_route, Route


def test_whitelist_forward():
    assert determine_route(100) == Route.WHITELIST_FORWARD
    assert determine_route(95) == Route.WHITELIST_FORWARD
    assert determine_route(90) == Route.WHITELIST_FORWARD


def test_ring_then_screen():
    assert determine_route(89) == Route.RING_THEN_SCREEN
    assert determine_route(75) == Route.RING_THEN_SCREEN
    assert determine_route(70) == Route.RING_THEN_SCREEN


def test_ai_screening():
    assert determine_route(69) == Route.AI_SCREENING
    assert determine_route(50) == Route.AI_SCREENING
    assert determine_route(30) == Route.AI_SCREENING


def test_spam_block():
    assert determine_route(29) == Route.SPAM_BLOCK
    assert determine_route(10) == Route.SPAM_BLOCK
    assert determine_route(0) == Route.SPAM_BLOCK
