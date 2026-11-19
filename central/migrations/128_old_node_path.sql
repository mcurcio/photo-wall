-- Decision 0034, S4c part 2a-v: the Pi's old HTTP path is gone (sessions, app links, the display
-- exchange, evidence, host observations and facts, the reconciler, app operations), so its 20 tables
-- go. One statement drops them whatever their foreign keys among themselves; after 127 no foreign key
-- from outside this list reaches them. Forward-only.
DROP TABLE node_reconciliation_work, node_evidence, node_display_withdrawals, node_display_handoffs,
           node_display_exchanges, node_reboot_commands, node_command_responses, node_app_effects,
           node_app_responses, node_app_links, node_manager_observations, node_host_observations,
           node_host_facts, node_sessions, node_producers, node_boot_admissions,
           node_environment_acceptances, node_app_qualification_samples, node_app_qualifications,
           node_app_operations;
