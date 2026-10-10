package com.acme.notes.util

class Clock { fun now(): Long = System.currentTimeMillis() }
class Stopwatch { fun elapsed(start: Long) = Clock().now() - start }
