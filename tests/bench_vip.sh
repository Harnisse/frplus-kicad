DEV=freerouting/dev/fr-master-01db81c.jar
python tests/bench.py vip_touch $DEV --only stickhub --set 'extra_args=["--router.autorouter.via_touching_pad=true"]'
python tests/bench.py vip_in $DEV --only stickhub --set 'extra_args=["--router.autorouter.via_in_pad=true","--router.autorouter.via_touching_pad=true"]'
python tests/bench.py vip_in $DEV --only opendrone --set 'extra_args=["--router.autorouter.via_in_pad=true","--router.autorouter.via_touching_pad=true"]'
echo ALL DONE
