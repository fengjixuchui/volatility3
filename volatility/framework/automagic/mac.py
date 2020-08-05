# This file is Copyright 2019 Volatility Foundation and licensed under the Volatility Software License 1.0
# which is available at https://www.volatilityfoundation.org/license/vsl-v1.0
#

import logging
import struct
from typing import Optional

from volatility.framework import interfaces, constants, layers
from volatility.framework.automagic import symbol_cache, symbol_finder
from volatility.framework.layers import intel, scanners
from volatility.framework.symbols import mac

vollog = logging.getLogger(__name__)


class MacIntelStacker(interfaces.automagic.StackerLayerInterface):
    stack_order = 45
    exclusion_list = ['windows', 'linux']

    @classmethod
    def stack(cls,
              context: interfaces.context.ContextInterface,
              layer_name: str,
              progress_callback: constants.ProgressCallback = None) -> Optional[interfaces.layers.DataLayerInterface]:
        """Attempts to identify mac within this layer."""
        # Bail out by default unless we can stack properly
        layer = context.layers[layer_name]
        new_layer = None
        join = interfaces.configuration.path_join

        # Never stack on top of an intel layer
        # FIXME: Find a way to improve this check
        if isinstance(layer, intel.Intel):
            return None

        mac_banners = MacBannerCache.load_banners()
        # If we have no banners, don't bother scanning
        if not mac_banners:
            vollog.info("No Mac banners found - if this is a mac plugin, please check your symbol files location")
            return None

        mss = scanners.MultiStringScanner([x for x in mac_banners if x])
        for banner_offset, banner in layer.scan(context = context, scanner = mss,
                                                progress_callback = progress_callback):
            dtb = None

            symbol_files = mac_banners.get(banner, None)
            if symbol_files:
                isf_path = symbol_files[0]
                table_name = context.symbol_space.free_table_name('MacintelStacker')
                table = mac.MacKernelIntermedSymbols(context = context,
                                                     config_path = join('temporary', table_name),
                                                     name = table_name,
                                                     isf_url = isf_path)
                context.symbol_space.append(table)
                kaslr_shift = cls.find_aslr(context = context,
                                            symbol_table = table_name,
                                            layer_name = layer_name,
                                            compare_banner = banner,
                                            compare_banner_offset = banner_offset,
                                            progress_callback = progress_callback)

                if kaslr_shift == 0:
                    vollog.log(constants.LOGLEVEL_VVV, "Invalid kalsr_shift found at offset: {}".format(banner_offset))
                    continue

                bootpml4_addr = cls.virtual_to_physical_address(table.get_symbol("BootPML4").address + kaslr_shift)

                new_layer_name = context.layers.free_layer_name("MacDTBTempLayer")
                config_path = join("automagic", "MacIntelHelper", new_layer_name)
                context.config[join(config_path, "memory_layer")] = layer_name
                context.config[join(config_path, "page_map_offset")] = bootpml4_addr

                layer = layers.intel.Intel32e(context,
                                              config_path = config_path,
                                              name = new_layer_name,
                                              metadata = {'os': 'Mac'})

                idlepml4_ptr = table.get_symbol("IdlePML4").address + kaslr_shift
                idlepml4_str = layer.read(idlepml4_ptr, 4)
                idlepml4_addr = struct.unpack("<I", idlepml4_str)[0]

                tmp_dtb = idlepml4_addr

                if tmp_dtb % 4096:
                    vollog.log(constants.LOGLEVEL_VVV, "Skipping non-page aligned DTB: 0x{:0x}".format(tmp_dtb))
                    continue

                dtb = tmp_dtb

                # Build the new layer
                new_layer_name = context.layers.free_layer_name("IntelLayer")
                config_path = join("automagic", "MacIntelHelper", new_layer_name)
                context.config[join(config_path, "memory_layer")] = layer_name
                context.config[join(config_path, "page_map_offset")] = dtb
                context.config[join(config_path, MacSymbolFinder.banner_config_key)] = str(banner, 'latin-1')

                new_layer = intel.Intel32e(context,
                                           config_path = config_path,
                                           name = new_layer_name,
                                           metadata = {'kaslr_value': kaslr_shift})

            if new_layer and dtb:
                vollog.debug("Identified banner: {}".format(repr(banner)))
                vollog.debug("DTB was found at: 0x{:0x}".format(dtb))
                return new_layer
        return None

    @classmethod
    def find_aslr(cls,
                  context: interfaces.context.ContextInterface,
                  symbol_table: str,
                  layer_name: str,
                  compare_banner: str = "",
                  compare_banner_offset: int = 0,
                  progress_callback: constants.ProgressCallback = None) -> int:
        """Determines the offset of the actual DTB in physical space and its
        symbol offset."""
        version_symbol = symbol_table + constants.BANG + 'version'
        version_json_address = context.symbol_space.get_symbol(version_symbol).address

        version_major_symbol = symbol_table + constants.BANG + 'version_major'
        version_major_json_address = context.symbol_space.get_symbol(version_major_symbol).address
        version_major_phys_offset = cls.virtual_to_physical_address(version_major_json_address)

        version_minor_symbol = symbol_table + constants.BANG + 'version_minor'
        version_minor_json_address = context.symbol_space.get_symbol(version_minor_symbol).address
        version_minor_phys_offset = cls.virtual_to_physical_address(version_minor_json_address)

        if not compare_banner_offset or not compare_banner:
            offset_generator = cls._scan_generator(context, layer_name, progress_callback)
        else:
            offset_generator = [(compare_banner_offset, compare_banner)]

        aslr_shift = 0

        for offset, banner in offset_generator:
            banner_major, banner_minor = [int(x) for x in banner[22:].split(b".")[0:2]]

            tmp_aslr_shift = offset - cls.virtual_to_physical_address(version_json_address)

            major_string = context.layers[layer_name].read(version_major_phys_offset + tmp_aslr_shift, 4)
            major = struct.unpack("<I", major_string)[0]

            if major != banner_major:
                continue

            minor_string = context.layers[layer_name].read(version_minor_phys_offset + tmp_aslr_shift, 4)
            minor = struct.unpack("<I", minor_string)[0]

            if minor != banner_minor:
                continue

            if tmp_aslr_shift & 0xfff != 0:
                continue

            aslr_shift = tmp_aslr_shift & 0xffffffff
            break

        vollog.log(constants.LOGLEVEL_VVVV, "Mac find_aslr returned: {:0x}".format(aslr_shift))

        return aslr_shift

    @classmethod
    def virtual_to_physical_address(cls, addr: int) -> int:
        """Converts a virtual mac address to a physical one (does not account
        of ASLR)"""
        if addr > 0xffffff8000000000:
            addr = addr - 0xffffff8000000000
        else:
            addr = addr - 0xff8000000000

        return addr

    @classmethod
    def _scan_generator(cls, context, layer_name, progress_callback):
        darwin_signature = rb"Darwin Kernel Version \d{1,3}\.\d{1,3}\.\d{1,3}: [^\x00]+\x00"

        for offset in context.layers[layer_name].scan(scanner = scanners.RegExScanner(darwin_signature),
                                                      context = context,
                                                      progress_callback = progress_callback):

            banner = context.layers[layer_name].read(offset, 128)

            idx = banner.find(b"\x00")
            if idx != -1:
                banner = banner[:idx]

            yield offset, banner


class MacBannerCache(symbol_cache.SymbolBannerCache):
    """Caches the banners found in the Mac symbol files."""
    os = "mac"
    symbol_name = "version"
    banner_path = constants.MAC_BANNERS_PATH


class MacSymbolFinder(symbol_finder.SymbolFinder):
    """Mac symbol loader based on uname signature strings."""

    banner_config_key = 'kernel_banner'
    banner_cache = MacBannerCache
    find_aslr = MacIntelStacker.find_aslr
    symbol_class = "volatility.framework.symbols.mac.MacKernelIntermedSymbols"
