#!/bin/bash

tail -n 50 logs/ingest.log | grep "\\[ingest\\] stats"
