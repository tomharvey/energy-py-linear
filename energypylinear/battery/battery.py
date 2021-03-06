from collections import OrderedDict
import json
import logging
from pulp import LpProblem, LpMinimize, lpSum, LpVariable, LpStatus


logger = logging.getLogger(__name__)

#  factor used to convert MW to MWh
#  MWh = MW / step
#  5min=12, 30min=2, 60min=1 etc

steps = {
    '5min': 60 / 5,
    '30min': 60 / 30,
    '60min': 1,
    '1hr': 1
}


class Battery(object):
    """Electric battery operating in price arbitrage.

    power       float [MW] same for charge & discharge
    capacity    float [MWh]
    efficiency  float [%] round trip, applied to
    """

    def __init__(self, power, capacity, efficiency=0.9):
        self.power = float(power)
        self.capacity = float(capacity)
        self.efficiency = float(efficiency)

        args = {
            "name": "args",
            "power": self.power,
            "capacity": self.capacity,
            "efficiency": self.efficiency,
        }

        logger.info(json.dumps(args))

    def setup_vars(self, idx):
        """Create a dictionary with the pulp variables."""
        return {
            'imports': LpVariable.dicts(
                'import', idx[:-1], lowBound=0,
                upBound=self.power, cat='Continuous'
            ),

            'exports': LpVariable.dicts(
                'export', idx[:-1], lowBound=0,
                upBound=self.power, cat='Continuous'
            ),

            'charges': LpVariable.dicts(
                'charge', idx, lowBound=0, cat='Continuous'
            ),

            'losses': LpVariable.dicts(
                'loss', idx, lowBound=0, cat='Continuous'
            )
        }

    def optimize(self, prices, forecasts=None, initial_charge=0, timestep='5min'):
        """Run the linear program to optimize the battery.

        prices         list [$/MWh]
        forecasts      list [$/MWh]
        initial_charge float [MWh]
        timestep       str   5min, 1hr etc
        """
        self.prob = LpProblem('cost minimization', LpMinimize)

        #  append a NaN onto the prices list to represent the price
        #  during the last reported period, which is only used to give the
        #  final charge, and not included in the optimization
        prices = list(prices)
        prices.append(None)

        self.timestep = timestep
        self.step = steps[self.timestep]

        if forecasts is None:
            forecasts = prices

        assert len(forecasts) == len(prices)
        assert initial_charge <= self.capacity
        assert initial_charge >= 0

        #  used to index timesteps
        idx = range(0, len(prices))

        self.vars = self.setup_vars(idx)

        imports = self.vars['imports']
        exports = self.vars['exports']
        charges = self.vars['charges']
        losses = self.vars['losses']

        #  the objective function we are minimizing
        self.prob += lpSum(
            [imports[i] * forecasts[i] for i in idx[:-1]] +
            [-exports[i] * forecasts[i] for i in idx[:-1]]
        )

        #  initial charge
        self.prob += charges[0] == initial_charge

        #  last item in the index isn't used because the last timestep only
        #  represents the final charge level - no import or export is done
        for i in idx[:-1]:
            #  energy balance across two time periods
            self.prob += charges[i + 1] == charges[i] + (imports[i] - exports[i] - losses[i]) / self.step

            #  constrain battery charge level
            self.prob += charges[i] <= self.capacity
            self.prob += charges[i] >= 0

            self.prob += losses[i] == exports[i] * (1 - self.efficiency)

        self.prob.solve()

        optimization_results = {
            "name": "optimization_results",
            "status": LpStatus[self.prob.status]
        }

        logger.info(json.dumps(optimization_results))

        self.info = self.generate_outputs(prices, forecasts, idx)

        return self.info

    def get_value_or_nan(self, var_value):
        """Get the value or assign None."""
        try:
            return var_value.value()
        except AttributeError:
            return None

    def calc_net(self, imp, exp, loss):
        """Calculate the Net, or None if inputs are None."""
        if None in [imp, exp, loss]:
            return None
        else:
            return imp - exp - loss

    def calc_cost(self, energy, price, step):
        """Calculate the cost, or None if energy is None."""
        if energy is None:
            return None
        return (energy * price) / step

    def calc_gross(self, imp, exp):
        """Calculate the Gross, or None if Import or Export are None."""
        try:
            return imp - exp
        except TypeError:
            return None

    def generate_outputs(self, prices, forecasts, idx_range):
        """Create a dictionary of results and summaries."""
        results = []
        for row_id in idx_range:
            imp = self.get_value_or_nan(self.vars['imports'].get(row_id))
            exp = self.get_value_or_nan(self.vars['exports'].get(row_id))
            loss = self.get_value_or_nan(self.vars['losses'].get(row_id))
            chg = self.get_value_or_nan(self.vars['charges'].get(row_id))
            price = prices[row_id]
            forecast = forecasts[row_id]

            net = self.calc_net(imp, exp, loss)
            actual_costs = self.calc_cost(net, price, self.step)
            forecast_costs = self.calc_cost(net, forecast, self.step)
            gross = self.calc_gross(imp, exp)

            result = [
                ('Import [MW]', imp),
                ('Export [MW]', exp),
                ('Gross [MW]', gross),
                ('Net [MW]', net),
                ('Losses [MW]', loss),
                ('Charge [MWh]', chg),
                ('Prices [$/MWh]', price),
                ('Forecast [$/MWh]', forecast),
                ('Actual [$/{}]'.format(self.timestep), actual_costs),
                ('Forecast [$/{}]'.format(self.timestep), forecast_costs)
            ]

            #  added ordered dict to get consistent iteration across results
            out = OrderedDict()
            for key, value in result:
                out[key] = value

            results.append(out)

        return results

if __name__ == '__main__':

    model = Battery(power=2, capacity=4)

    prices = [50, 10, 10, 50, 50, 10]

    info = model.optimize(prices, initial_charge=1, timestep='1hr')
