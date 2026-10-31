/*
 * SDRAM init applet for the SAM-BA monitor (see docs/BOARD.md).
 *
 * Loaded into internal SRAM and called with the monitor's G command, which
 * sets LR, so this returns to the monitor. Same register values and order as
 * the stock at91bootstrap (0x374, 0x924), but the clocks are left as the
 * ROM set them (MCK 48 MHz), so the refresh timer is rescaled:
 * 7.8 us * 48 MHz = 374 = 0x176 (stock: 0x40b at 127 MHz).
 */
	.arm
	.text
	.global _start
_start:
	push	{r4, r5, lr}

	ldr	r0, =0xFFFFDF20		@ CCFG_EBICSA: CS1 -> DDR/SDR controller
	ldr	r1, =0x302
	str	r1, [r0]
	ldr	r0, =0xFFFFFC00		@ PMC_SCER: DDRCK on
	mov	r1, #4
	str	r1, [r0]

	ldr	r4, =0xFFFFE800		@ DDRSDRC0
	ldr	r5, =0x20000000		@ SDRAM
	mov	r2, #0

	ldr	r1, =0x11		@ MDR: low-power SDR, 16-bit
	str	r1, [r4, #0x20]
	ldr	r1, =0x00400339		@ CR: 9 col, 13 row, CAS 3, 4 banks
	str	r1, [r4, #0x08]
	ldr	r1, =0x3234a346		@ T0PR
	str	r1, [r4, #0x0c]
	ldr	r1, =0x05c8080b		@ T1PR
	str	r1, [r4, #0x10]
	ldr	r1, =0x1206		@ LPR
	str	r1, [r4, #0x1c]
	ldr	r0, =0x341c
	bl	delay

	mov	r1, #1			@ NOP command
	str	r1, [r4]
	str	r2, [r5]
	ldr	r0, =0x33f6
	bl	delay

	mov	r1, #2			@ precharge all banks
	str	r1, [r4]
	str	r2, [r5, #0x08]

	mov	r1, #4			@ auto refresh, twice
	str	r1, [r4]
	str	r2, [r5, #0x10]
	str	r1, [r4]
	str	r2, [r5, #0x20]

	mov	r1, #3			@ load mode register
	str	r1, [r4]
	str	r2, [r5]

	str	r2, [r4]		@ normal mode
	mov	r1, #0x12
	str	r1, [r5, #0x48]

	ldr	r1, =0x176		@ RTR for MCK 48 MHz
	str	r1, [r4, #0x04]

	pop	{r4, r5, pc}

delay:
	subs	r0, r0, #1
	bne	delay
	bx	lr

	.ltorg
