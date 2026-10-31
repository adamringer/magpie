# Sourced helper: GPIO lines by pin name, via the 6.18 per-chip sysfs ABI
# (/sys/class/gpio/chipN/{label,export}; lines appear as chipN/gpio<offset>/).
# gpio_line <A|B|C|D> <pin>  -> prints the line's sysfs directory, exporting it if needed
gpio_line() {
	case "$1" in
	A) addr=fffff400 ;; B) addr=fffff600 ;; C) addr=fffff800 ;; D) addr=fffffa00 ;;
	*) return 1 ;;
	esac
	for c in /sys/class/gpio/chip*; do
		[ -r "$c/label" ] || continue
		l=$(cat "$c/label" 2>/dev/null)  # BusyBox read returns empty on sysfs here
		case "$l" in
		*"$addr"*)
			[ -d "$c/gpio$2" ] || echo "$2" > "$c/export" 2>/dev/null
			[ -d "$c/gpio$2" ] || return 1
			echo "$c/gpio$2"
			return 0 ;;
		esac
	done
	return 1
}
