from inspect import isclass
from hearthstone.enums import CardType, Mulligan, PlayState, Zone
from .dsl import LazyNum, LazyValue, Picker, Selector
from .entity import Entity
from .logging import log


def _eval_card(source, card):
	"""
	Return a Card instance from \a card
	The card argument can be:
	- A Card instance (nothing is done)
	- The string ID of the card (the card is created)
	- A Picker instance (a card is dynamically picked)
	"""
	if isinstance(card, Picker):
		card = card.pick(source)

	if isinstance(card, LazyValue):
		card = card.evaluate(source)

	if isinstance(card, Action):
		card = card.trigger(source)[0]

	if not isinstance(card, list):
		cards = [card]
	else:
		cards = card

	ret = []
	for card in cards:
		if isinstance(card, str):
			ret.append(source.controller.card(card, source))
		else:
			ret.append(card)

	return ret


class EventListener:
	ON = 1
	AFTER = 2

	def __init__(self, trigger, actions, at):
		self.trigger = trigger
		self.actions = actions
		self.at = at
		self.once = False

	def __repr__(self):
		return "<EventListener %r>" % (self.trigger)


class Action:  # Lawsuit
	ARGS = ()

	def __init__(self, *args, **kwargs):
		self._args = args
		self._kwargs = kwargs
		self.callback = ()
		self.times = 1

	def __repr__(self):
		args = ["%s=%r" % (k, v) for k, v in zip(self.ARGS, self._args)]
		return "<Action: %s(%s)>" % (self.__class__.__name__, ", ".join(args))

	def after(self, *actions):
		return EventListener(self, actions, EventListener.AFTER)

	def on(self, *actions):
		return EventListener(self, actions, EventListener.ON)

	def then(self, *args):
		"""
		Create a callback containing an action queue, called upon the
		action's trigger with the action's arguments available.
		"""
		ret = self.__class__(*self._args, **self._kwargs)
		ret.callback = args
		ret.times = self.times
		return ret

	def _broadcast(self, entity, source, at, *args):
		for event in entity.events:
			if event.at != at:
				continue
			if isinstance(event.trigger, self.__class__) and event.trigger.matches(entity, args):
				log.info("%r triggers off %r from %r", entity, self, source)
				entity.trigger_event(source, event, args)

	def broadcast(self, source, at, *args):
		for entity in source.game.entities:
			self._broadcast(entity, source, at, *args)

		for entity in source.game.hands:
			self._broadcast(entity, source, at, *args)

	def get_args(self, source):
		return self._args

	def matches(self, source, args):
		for arg, match in zip(args, self._args):
			if match is None:
				# Allow matching Action(None, None, z) to Action(x, y, z)
				continue
			# this stuff is stupidslow
			res = match.eval([arg], source)
			if not res or res[0] is not arg:
				return False
		return True


class ActionArg(LazyValue):
	"""
	The argument passed to an Action, lazily evaluated
	"""
	def __init__(self, name, index, cls):
		self.name = name
		self.index = index
		self.cls = cls

	def __repr__(self):
		return "<%s.%s>" % (self.cls.__name__, self.name)

	def evaluate(self, source):
		# This is used when an event listener triggers and the callback
		# Action has arguments of the type Action.FOO
		# XXX we rely on source.event_args to be set, but it's very racey.
		# If multiple events happen on an entity at once, stuff will go wrong.
		assert source.event_args
		return source.event_args[self.index]


class GameAction(Action):
	def trigger(self, source):
		args = self.get_args(source)
		source.game.manager.action(self, source, *args)
		self.do(source, *args)
		source.game.manager.action_end(self, source, *args)
		source.game.process_deaths()


class Attack(GameAction):
	"""
	Make \a ATTACKER attack \a DEFENDER
	"""
	ARGS = ("ATTACKER", "DEFENDER")

	def get_args(self, source):
		ret = super().get_args(source)
		return ret

	def do(self, source, attacker, defender):
		attacker.attack_target = defender
		defender.defending = True
		source.game.proposed_attacker = attacker
		source.game.proposed_defender = defender
		log.info("%r attacks %r", attacker, defender)
		self.broadcast(source, EventListener.ON, attacker, defender)

		defender = source.game.proposed_defender
		source.game.proposed_attacker = None
		source.game.proposed_defender = None
		if attacker.should_exit_combat:
			log.info("Attack has been interrupted.")
			attacker.attack_target = None
			defender.defending = False
			return

		assert attacker is not defender, "Why are you hitting yourself %r?" % (attacker)

		# Save the attacker/defender atk values in case they change during the attack
		# (eg. in case of Enrage)
		def_atk = defender.atk
		source.game.queue_actions(attacker, [Hit(defender, attacker.atk)])
		if def_atk:
			source.game.queue_actions(defender, [Hit(attacker, def_atk)])

		self.broadcast(source, EventListener.AFTER, attacker, defender)

		attacker.attack_target = None
		defender.defending = False
		attacker.num_attacks += 1


class BeginTurn(GameAction):
	"""
	Make \a player begin the turn
	"""
	ARGS = ("PLAYER", )

	def do(self, source, player):
		self.broadcast(source, EventListener.ON, player)
		source.game._begin_turn(player)


class Concede(GameAction):
	"""
	Make \a player concede
	"""
	ARGS = ("PLAYER", )

	def do(self, source, player):
		player.playstate = PlayState.QUIT


class Deaths(GameAction):
	"""
	Process all deaths in the PLAY Zone.
	"""
	def do(self, source, *args):
		source.game.process_deaths()


class Death(GameAction):
	"""
	Move target to the GRAVEYARD Zone.
	"""
	ARGS = ("ENTITY", )

	def do(self, source, target):
		log.info("Processing Death for %r", target)
		self.broadcast(source, EventListener.ON, target)
		if target.deathrattles:
			source.game.queue_actions(source, [Deathrattle(target)])


class EndTurn(GameAction):
	"""
	End the current turn
	"""
	ARGS = ("PLAYER", )

	def do(self, source, player):
		assert not player.choice, "Attempted to end a turn with a choice open"
		self.broadcast(source, EventListener.ON, player)
		source.game._end_turn()


class Joust(GameAction):
	"""
	Perform a joust between \a challenger and \a defender.
	Note that this does not evaluate the results of the joust. For that,
	see dsl.evaluators.JoustEvaluator.
	"""
	ARGS = ("CHALLENGER", "DEFENDER")

	def get_args(self, source):
		challenger = self._args[0].eval(source.game, source)
		defender = self._args[1].eval(source.game, source)
		return challenger and challenger[0], defender and defender[0]

	def do(self, source, challenger, defender):
		log.info("Jousting %r vs %r", challenger, defender)
		for action in self.callback:
			log.debug("%r joust callback: %r", self, action)
			source.game.queue_actions(source, [action], event_args=[challenger, defender])


class GenericChoice(GameAction):
	ARGS = ("PLAYER", "CARDS")

	def get_args(self, source):
		player = self._args[0]
		if isinstance(player, Selector):
			player = player.eval(source.game.players, source)
			assert len(player) == 1
			player = player[0]
		cards = self._args[1]
		if isinstance(cards, Selector):
			cards = cards.eval(source.game, source)
		return player, cards

	def do(self, source, player, cards):
		player.choice = self
		self.player = player
		self.cards = cards

	def choose(self, card):
		for _card in self.cards:
			if _card is card and len(self.player.hand) < self.player.max_hand_size:
				_card.zone = Zone.HAND
			else:
				_card.discard()


class MulliganChoice(GameAction):
	ARGS = ("PLAYER", )

	def do(self, source, player):
		player.mulligan_state = Mulligan.INPUT
		player.choice = self
		# NOTE: Ideally, we give The Coin when the Mulligan is over.
		# Unfortunately, that's not compatible with Blizzard's way.
		self.cards = player.hand.exclude(id="GAME_005")
		self.player = player

	def choose(self, *cards):
		self.player.draw(len(cards))
		for card in cards:
			assert card in self.cards
			card.zone = Zone.DECK
		self.player.choice = None
		self.player.shuffle_deck()
		self.player.mulligan_state = Mulligan.DONE


class Play(GameAction):
	"""
	Make the source player play \a card, on \a target or None.
	Choose play action from \a choose or None.
	"""
	ARGS = ("PLAYER", "CARD", "TARGET", "CHOOSE")

	def _broadcast(self, entity, source, at, *args):
		# Prevent cards from triggering off their own play
		if entity is args[1]:
			return
		return super()._broadcast(entity, source, at, *args)

	def get_args(self, source):
		return (source, ) + super().get_args(source)

	def do(self, source, player, card, target, index):
		source_card = card
		play_action = card.action

		if card.parent_card:
			# Get the "main" card from the Choose One
			card.parent_card.choose = card
			card = card.parent_card

		card.target = target
		card._summon_index = index
		source_card.target = target
		player.game.no_aura_refresh = True
		player.game._play(card)

		self.broadcast(player, EventListener.ON, player, card, target)
		# NOTE: A Play is not a summon! But it sure looks like one.
		# We need to fake a Summon broadcast.
		summon_action = Summon(player, card)
		summon_action.broadcast(player, EventListener.ON, player, card)
		player.game.no_aura_refresh = False
		player.game.refresh_auras()

		# "Can't Play" (aka Counter) means triggers don't happen either
		if not card.cant_play:
			# Battlecry etc
			play_action()

			# If the play action transforms the card (eg. Druid of the Claw), we
			# have to broadcast the morph result as minion instead.
			if card.morphed:
				played_minion = card.morphed
			else:
				played_minion = card
			summon_action.broadcast(player, EventListener.AFTER, player, played_minion)
			self.broadcast(player, EventListener.AFTER, player, played_minion, target)

		player.combo = True
		player.cards_played_this_turn += 1
		if source_card.type == CardType.MINION:
			player.minions_played_this_turn += 1

		card.target = None
		source_card.target = None
		card.choose = None


class Activate(GameAction):
	ARGS = ("PLAYER", "CARD", "TARGET")

	def get_args(self, source):
		return (source, ) + super().get_args(source)

	def do(self, source, player, heropower, target=None):
		ret = []

		self.broadcast(source, EventListener.ON, player, heropower, target)

		actions = heropower.get_actions("activate")
		if actions:
			ret += source.game.queue_actions(heropower, actions)

		for minion in player.field.filter(has_inspire=True):
			actions = minion.get_actions("inspire")
			if actions is None:
				raise NotImplementedError("Missing inspire script for %r" % (minion))
			if actions:
				ret += source.game.queue_actions(minion, actions)

		return ret


class TargetedAction(Action):
	ARGS = ("TARGETS", )

	def __init__(self, *args, **kwargs):
		self.source = kwargs.pop("source", None)
		super().__init__(*args, **kwargs)
		self.event_queue = []

	def __repr__(self):
		args = ["%s=%r" % (k, v) for k, v in zip(self.ARGS[1:], self._args[1:])]
		return "<TargetedAction: %s(%s)>" % (self.__class__.__name__, ", ".join(args))

	def __mul__(self, value):
		self.times = value
		return self

	def eval(self, selector, source):
		if isinstance(selector, Entity):
			return [selector]
		else:
			return selector.eval(source.game, source)

	def get_target_args(self, source, target):
		ret = []
		for k, v in zip(self.ARGS, self._args):
			if k == "TARGETS":
				continue
			elif isinstance(v, Selector):
				# evaluate Selector arguments
				v = v.eval(source.game, source)
			elif isinstance(v, LazyValue):
				v = v.evaluate(source)
			elif k.startswith("CARD"):
				# HACK: card-likes are always named CARDS
				v = _eval_card(source, v)
			ret.append(v)
		return ret

	def get_targets(self, source, t):
		if isinstance(t, Entity):
			ret = t
		elif isinstance(t, LazyValue):
			ret = t.evaluate(source)
		else:
			ret = t.eval(source.game, source)
		if not hasattr(ret, "__iter__"):
			# Bit of a hack to ensure we always get a list back
			ret = [ret]
		return ret

	def trigger(self, source):
		ret = []

		if self.source is not None:
			source = self.source.eval(source.game, source)
			assert len(source) == 1
			source = source[0]

		times = self.times
		if isinstance(times, LazyValue):
			times = times.evaluate(source)
		elif isinstance(times, Action):
			times = times.trigger(source)[0]

		for i in range(times):
			args = self.get_args(source)
			targets = self.get_targets(source, args[0])
			args = args[1:]
			source.game.manager.action(self, source, targets, *args)
			log.info("%r triggering %r targeting %r", source, self, targets)
			for target in targets:
				target_args = self.get_target_args(source, target)
				ret.append(self.do(source, target, *target_args))

				for action in self.callback:
					log.info("%r queues up callback %r", self, action)
					ret += source.game.queue_actions(source, [action], event_args=[target] + target_args)

			source.game.manager.action_end(self, source, targets, *self._args)

		for args in self.event_queue:
			self.broadcast(*args)
		self.event_queue = []

		return ret


class Buff(TargetedAction):
	"""
	Buff character targets with Enchantment \a id
	"""
	ARGS = ("TARGETS", "BUFF")

	def do(self, source, target, buff):
		kwargs = self._kwargs.copy()
		for k, v in kwargs.items():
			if isinstance(v, LazyNum):
				kwargs[k] = v.evaluate(source)
		return source.buff(target, buff, **kwargs)


class SwapAttackAndHealth(Buff):
	def do(self, source, target, buff):
		log.info("%r swaps attack and health for %r", source, target)
		buff = super().do(source, target, buff)
		atk = target.health - target.atk
		health = target.atk - target.health
		buff._atk = atk
		buff._max_health = health
		target.damage = 0
		return buff


class Bounce(TargetedAction):
	"""
	Bounce minion targets on the field back into the hand.
	"""
	def do(self, source, target):
		if len(target.controller.hand) >= target.controller.max_hand_size:
			log.info("%r is bounced to a full hand and gets destroyed", target)
			return source.game.queue_actions(source, [Destroy(target)])
		else:
			log.info("%r is bounced back to %s's hand", target, target.controller)
			target.zone = Zone.HAND


class Counter(TargetedAction):
	"""
	Counter a card, making it unplayable.
	"""
	def do(self, source, target):
		target.cant_play = True


class Predamage(TargetedAction):
	"""
	Predamage target by \a amount.
	"""
	ARGS = ("TARGETS", "AMOUNT")

	def do(self, source, target, amount):
		target.predamage = amount
		if amount:
			self.broadcast(source, EventListener.ON, target, amount)
			return source.game.trigger_actions(source, [Damage(target, amount)])[0][0]


class Damage(TargetedAction):
	"""
	Damage target by \a amount.
	"""
	ARGS = ("TARGETS", "AMOUNT")

	def do(self, source, target, amount):
		amount = target._hit(source, target.predamage)
		target.predamage = 0
		if source.type == CardType.MINION and source.stealthed:
			# TODO this should be an event listener of sorts
			source.stealthed = False
		if amount:
			self.broadcast(source, EventListener.ON, target, amount, source)
		return amount


class Deathrattle(TargetedAction):
	"""
	Trigger deathrattles on card targets.
	"""
	def do(self, source, target):
		for deathrattle in target.deathrattles:
			if callable(deathrattle):
				actions = deathrattle(target)
			else:
				actions = deathrattle
			source.game.queue_actions(target, actions)

			if target.controller.extra_deathrattles:
				log.info("Triggering deathrattles for %r again", target)
				source.game.queue_actions(target, actions)


class Destroy(TargetedAction):
	"""
	Destroy character targets.
	"""
	def do(self, source, target):
		target._destroy()


class Discard(TargetedAction):
	"""
	Discard card targets in a player's hand
	"""
	def do(self, source, target):
		self.broadcast(source, EventListener.ON, target)
		target.discard()


class Draw(TargetedAction):
	"""
	Make player targets draw a card from their deck.
	"""
	ARGS = ("TARGETS", "CARD")

	def get_target_args(self, source, target):
		if target.deck:
			card = target.deck[-1]
		else:
			card = None
		return [card]

	def do(self, source, target, card):
		if card is None:
			target.fatigue()
			return []
		card.draw()
		self.broadcast(source, EventListener.ON, target, card, source)

		return [card]


class Fatigue(TargetedAction):
	"""
	Hit a player with a tick of fatigue
	"""
	ARGS = ("TARGETS", )

	def do(self, source, target):
		if target.cant_fatigue:
			log.info("%s can't fatigue and does not take damage", target)
			return
		target.fatigue_counter += 1
		log.info("%s takes %i fatigue damage", target, target.fatigue_counter)
		return source.game.queue_actions(source, [Hit(target.hero, target.fatigue_counter)])


class ForceDraw(TargetedAction):
	"""
	Draw card targets into their owners hand
	"""
	def do(self, source, target):
		target.draw()


class FullHeal(TargetedAction):
	"""
	Fully heal character targets.
	"""
	def do(self, source, target):
		source.heal(target, target.max_health)


class GainArmor(TargetedAction):
	"""
	Make hero targets gain \a amount armor.
	"""
	ARGS = ("TARGETS", "AMOUNT")

	def do(self, source, target, amount):
		target.armor += amount
		self.broadcast(source, EventListener.ON, target, amount)


class GainMana(TargetedAction):
	"""
	Give player targets \a Mana crystals.
	"""
	ARGS = ("TARGETS", "AMOUNT")

	def do(self, source, target, amount):
		target.max_mana += amount


class GainEmptyMana(TargetedAction):
	"""
	Give player targets \a amount empty Mana crystals.
	"""
	ARGS = ("TARGETS", "AMOUNT")

	def do(self, source, target, amount):
		target.max_mana += amount
		target.used_mana += amount


class Give(TargetedAction):
	"""
	Give player targets card \a id.
	"""
	ARGS = ("TARGETS", "CARD")

	def do(self, source, target, cards):
		log.info("Giving %r to %s", cards, target)
		ret = []
		if not hasattr(cards, "__iter__"):
			# Support Give on multiple cards at once (eg. Echo of Medivh)
			cards = [cards]
		for card in cards:
			if len(target.hand) >= target.max_hand_size:
				log.info("Give(%r) fails because %r's hand is full", card, target)
				continue
			card.controller = target
			card.zone = Zone.HAND
			ret.append(card)
		return ret


class Hit(TargetedAction):
	"""
	Hit character targets by \a amount.
	"""
	ARGS = ("TARGETS", "AMOUNT")

	def do(self, source, target, amount):
		amount = source.get_damage(amount, target)
		if amount:
			return source.game.queue_actions(source, [Predamage(target, amount)])[0][0]


class Heal(TargetedAction):
	"""
	Heal character targets by \a amount.
	"""
	ARGS = ("TARGETS", "AMOUNT")

	def do(self, source, target, amount):
		if source.controller.outgoing_healing_adjustment:
			# "healing as damage" (hack-ish)
			return source.game.queue_actions(source, [Hit(target, amount)])

		amount *= (source.controller.healing_double + 1)
		amount = min(amount, target.damage)
		if amount:
			# Undamaged targets do not receive heals
			log.info("%r heals %r for %i", source, target, amount)
			target.damage -= amount
			self.event_queue.append((source, EventListener.ON, target, amount))


class ManaThisTurn(TargetedAction):
	"""
	Give player targets \a amount Mana this turn.
	"""
	ARGS = ("TARGETS", "AMOUNT")

	def do(self, source, target, amount):
		target.temp_mana += min(target.max_resources - target.mana, amount)


class Mill(TargetedAction):
	"""
	Mill \a count cards from the top of the player targets' deck.
	"""
	ARGS = ("TARGETS", "AMOUNT")

	def do(self, source, target, count):
		target.mill(count)


class Morph(TargetedAction):
	"""
	Morph minion target into \a minion id
	"""
	ARGS = ("TARGETS", "CARD")

	def get_target_args(self, source, target):
		card = _eval_card(source, self._args[1])
		assert len(card) == 1
		card = card[0]
		card.controller = target.controller
		return [card]

	def do(self, source, target, card):
		log.info("Morphing %r into %r", target, card)
		target.clear_buffs()
		target_zone = target.zone
		target.zone = Zone.SETASIDE
		if card.zone != target_zone:
			# In-place morph is OK, eg. in the case of Lord Jaraxxus
			card.zone = target_zone
		target.morphed = card
		return card


class FillMana(TargetedAction):
	"""
	Refill \a amount mana crystals from player targets.
	"""
	ARGS = ("TARGETS", "AMOUNT")

	def do(self, source, target, amount):
		target.used_mana -= amount


class Retarget(TargetedAction):
	ARGS = ("TARGETS", "CARDS")

	def do(self, source, target, new_target):
		assert len(new_target) == 1
		new_target = new_target[0]
		if target.type in (CardType.HERO, CardType.MINION) and target.attacking:
			log.info("Retargeting %r's attack to %r", source, new_target)
			source.game.proposed_defender.defending = False
			source.game.proposed_defender = new_target
		else:
			log.info("Retargeting %r from %r to %r", target, target.target, new_target)
			target.target = new_target

		return new_target


class Reveal(TargetedAction):
	"""
	Reveal secret targets.
	"""
	def do(self, source, target):
		log.info("Revealing secret %r", target)
		self.broadcast(source, EventListener.ON, target)
		target.zone = Zone.GRAVEYARD


class SetCurrentHealth(TargetedAction):
	"""
	Sets the current health of the character target to \a amount.
	"""
	ARGS = ("TARGETS", "AMOUNT")

	def do(self, source, target, amount):
		log.info("Setting current health on %r to %i", target, amount)
		maxhp = target.max_health
		target.damage = max(0, maxhp - amount)


class SetTag(TargetedAction):
	"""
	Sets targets' given tags.
	"""
	ARGS = ("TARGETS", "TAGS")

	def do(self, source, target, tags):
		if isinstance(tags, dict):
			for tag, value in tags.items():
				target.tags[tag] = value
		else:
			for tag in tags:
				target.tags[tag] = True


class UnsetTag(TargetedAction):
	"""
	Unset targets' given tags.
	"""
	ARGS = ("TARGETS", "TAGS")

	def do(self, source, target, tags):
		for tag in tags:
			target.tags[tag] = False


class Silence(TargetedAction):
	"""
	Silence minion targets.
	"""
	def do(self, source, target):
		log.info("Silencing %r", self)
		self.broadcast(source, EventListener.ON, target)

		target.clear_buffs()
		for attr in target.silenceable_attributes:
			if getattr(target, attr):
				setattr(target, attr, False)

		# Wipe the event listeners
		target._events = []
		target.silenced = True


class Summon(TargetedAction):
	"""
	Make player targets summon \a id onto their field.
	This works for equipping weapons as well as summoning minions.
	"""
	ARGS = ("TARGETS", "CARDS")

	def _broadcast(self, entity, source, at, *args):
		# Prevent cards from triggering off their own summon
		if entity is args[1]:
			return
		return super()._broadcast(entity, source, at, *args)

	def do(self, source, target, cards):
		log.info("%s summons %r", target, cards)
		if not isinstance(cards, list):
			cards = [cards]

		for card in cards:
			if not card.is_summonable():
				continue
			if card.controller != target:
				card.controller = target
			self.broadcast(source, EventListener.ON, target, card)
			if card.zone != Zone.PLAY:
				card.zone = Zone.PLAY
			self.broadcast(source, EventListener.AFTER, target, card)

		return cards


class Shuffle(TargetedAction):
	"""
	Shuffle card targets into player target's deck.
	"""
	ARGS = ("TARGETS", "CARDS")

	def do(self, source, target, cards):
		log.info("%r shuffles into %s's deck", cards, target)
		if not isinstance(cards, list):
			cards = [cards]

		for card in cards:
			if card.controller != target:
				card.controller = target
			card.zone = Zone.DECK
			target.shuffle_deck()


class Swap(TargetedAction):
	"""
	Swap minion target with \a other.
	Behaviour is undefined when swapping more than two minions.
	"""
	ARGS = ("TARGETS", "OTHER")

	def get_target_args(self, source, target):
		other = self.eval(self._args[1], source)
		if not other:
			return (None, )
		assert len(other) == 1
		return [other[0]]

	def do(self, source, target, other):
		if other is not None:
			orig = target.zone
			target.zone = other.zone
			other.zone = orig


class Steal(TargetedAction):
	"""
	Make the controller take control of targets.
	The controller is the controller of the source of the action.
	"""
	ARGS = ("TARGETS", "CONTROLLER")

	def get_target_args(self, source, target):
		if len(self._args) > 1:
			# Controller was specified
			controller = self.eval(self._args[1], source)
			assert len(controller) == 1
			controller = controller[0]
		else:
			# Default to the source's controller
			controller = source.controller
		return [controller]

	def do(self, source, target, controller):
		log.info("%s takes control of %r", controller, target)
		zone = target.zone
		target.zone = Zone.SETASIDE
		target.controller = controller
		target.turns_in_play = 0  # To ensure summoning sickness
		target.zone = zone


class UnlockOverload(TargetedAction):
	"""
	Unlock the target player's overload, both current and owed.
	"""
	def do(self, source, target):
		log.info("%s overload gets cleared", target)
		target.overloaded = 0
		target.overload_locked = 0


# Register the action arguments as attributes to the action
d = globals().copy()
for k, v in d.items():
	if isclass(v) and issubclass(v, Action):
		for i, arg in enumerate(v.ARGS):
			setattr(v, arg, ActionArg(arg, i, v))
