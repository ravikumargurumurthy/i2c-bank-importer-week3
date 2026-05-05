EVAL_SET = [
    # ===== VIN path: clean auto-apply =====
    {
        "id": "ev_B001_van_clean_match_hapag",
        "description": "VIN present, matches customer master cleanly",
        "bank_txn_id": "27e31459-a03c-4616-a539-fdc515d6ad9d",
        "expected": {
            "match_method": "vin_exact",
            "min_confidence": 0.95,
            "max_confidence": 1.0,
            "routing_decision": "auto_apply",
            "payer_customer_name_contains": "HAPAG",
        },
    },
    {
        "id": "ev_B002_van_clean_match_hapag_alt",
        "description": "Another HAPAG payment with different VIN",
        "bank_txn_id": "11b111df-77a3-4610-a45c-2f5e6d80698c",
        "expected": {
            "match_method": "vin_exact",
            "min_confidence": 0.95,
            "max_confidence": 1.0,
            "routing_decision": "auto_apply",
            "payer_customer_name_contains": "HAPAG",
        },
    },

    # ===== No-VIN path: defer to downstream matching =====
    {
        "id": "ev_B003_no_van_awaiting_remittance",
        "description": (
            "No VIN present. Per SME-informed workflow, the bank-extraction "
            "agent does NOT attempt customer identification at this stage. "
            "It emits structured signals (UTR, payment_mode, narrative) and "
            "defers customer identification to the downstream matching agent, "
            "which will use the remittance document."
        ),
        "bank_txn_id": "bd6c613c-dbee-49b6-9d2c-610ed6c985a1",
        "expected": {
            "match_method": "awaiting_remittance",
            "min_confidence": 0.30,
            "max_confidence": 0.50,
            "routing_decision": "awaiting_remittance",
            "payer_customer_number_is_null": True,
            "bank_utr_is_set": True,
            "payment_mode_is_set": True,
        },
    },
    {
        "id": "ev_B004_no_van_awaiting_remittance_alt",
        "description": "Another no-VIN payment; same expected behavior",
        "bank_txn_id": "cc5bf181-4ad4-4e55-9e17-5e62dca58140",
        "expected": {
            "match_method": "awaiting_remittance",
            "min_confidence": 0.30,
            "max_confidence": 0.50,
            "routing_decision": "awaiting_remittance",
            "payer_customer_number_is_null": True,
            "bank_utr_is_set": True,
            "payment_mode_is_set": True,
        },
    },
    {
    "id": "ev_B005_unparseable_bank_fee_reversal",
    "description": (
        "Bank fee reversal ('Rev.of DD Chrgs'), not a customer payment. "
        "No NEFT format, no UTR, no VIN, no payer name. Agent should "
        "recognize this is unparseable and route to exception."
    ),
    "bank_txn_id": "1d8126d8-9daa-42ff-adc3-6837cffe5986",
    "expected": {
        "match_method": "unparseable",
        "min_confidence": 0.0,
        "max_confidence": 0.30,
        "routing_decision": "exception",
        "payer_customer_number_is_null": True,
    },
},
]