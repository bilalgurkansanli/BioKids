# The BioKids image (D-249), on a Debian 13 machine with live-build:
#
#     sudo apt install live-build
#     sudo make DEBS=../dist/2.0.0-1          # the .debs dpkg-buildpackage made
#
# The result is biokids-2.0.0-amd64.hybrid.iso, for a USB stick or a DVD. Every
# build is 30-60 minutes; `sudo make clean` before building with changed config.

DEBS ?= ../dist/2.0.0-1
# Rebuilt for Trixie (dist/ai, not in git): from the shared folder on the build VM.
AI_DEBS ?= /media/sf_BioKids/dist/ai

.PHONY: iso clean

iso:
	rm -rf config/packages.chroot
	mkdir -p config/packages.chroot
	cp $(DEBS)/biokids*_all.deb config/packages.chroot/
	cp $(AI_DEBS)/*.deb config/packages.chroot/
	lb config
	lb build
	sha256sum *.iso > SHA256SUMS

clean:
	lb clean --purge
	rm -rf config/packages.chroot SHA256SUMS
